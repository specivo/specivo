"""Processing pull/merge request deliveries: store the request, link it to issues, run merge automation.

Called by :func:`specivo.services.repository_hooks.process_delivery` for an
accepted ``pull_request`` delivery (GitHub and Gitea/Forgejo
``pull_request``, GitLab ``Merge Request Hook``):

1. Parse the event with the provider adapter and refresh the default branch.
2. Find the in-scope issues the title, body and source branch reference
   (:func:`specivo.hooks.pull_requests.pull_request_links`).
3. Upsert the ``pull_requests`` row, unless the stored version is newer
   (:func:`specivo.hooks.pull_requests.is_newer`): events that arrive out of
   order never move the state backwards. A request that references nothing
   in scope and was never stored is not stored, unless it is merged: a later
   edit then finds it merged already.
4. Insert the links; a newly linked issue gets one ``$git`` journal, and the
   request URL and source branch appended to its ``pull_requests`` and
   ``branches`` metadata (no duplicates). Until the request is merged, each
   link's ``closing`` follows the current text: a reference that was dropped
   or lost its fixing keyword is no longer closing.
5. When this delivery is the merge (the stored request goes from another
   state to merged, or a first-seen request arrives with the merge action
   itself), its closing links are marked ``merge_qualified_at``. Links added
   or made closing by editing the text after the merge, including an edit
   that is the first event seen of an already merged request, never qualify.
6. A merge into the default branch with merge automation on moves each
   qualified link's issue once (``merge_handled_at``), while the issue's
   project is still in the integration's scope; see
   :mod:`specivo.services.repository_commands`.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.utils import utcnow
from specivo.hooks.providers import PullRequestEvent, get_provider
from specivo.hooks.pull_requests import PullRequestLink, is_merge_event, is_newer, pull_request_links
from specivo.models.issue import Issue
from specivo.models.repository import (
    DELIVERY_IGNORED,
    DELIVERY_PROCESSED,
    PR_MERGED,
    IssuePullRequestLink,
    PullRequest,
    RepositoryIntegration,
)
from specivo.services import repository_commands as commands

#: Issue metadata keys pull request URLs and branch names are appended to.
PULL_REQUESTS_METADATA_KEY = "pull_requests"
BRANCHES_METADATA_KEY = "branches"

_FIELD_MAX = {"url": 2048, "source_branch": 255, "target_branch": 255, "author_login": 255, "merged_by_login": 255}


def pr_label(provider: str, pr: PullRequest) -> str:
    """``Pull request [#7](url)``, or ``Merge request [!7](url)`` for GitLab, safe for Markdown."""
    from specivo.services.repository_hooks import safe_url

    noun, sigil = ("Merge request", "!") if provider == "gitlab" else ("Pull request", "#")
    url = safe_url(pr.url)
    number = f"{sigil}{pr.number}"
    return f"{noun} [{number}]({url})" if url else f"{noun} {number}"


def pr_journal_note(provider: str, pr: PullRequest) -> str:
    """The journal text for a newly linked pull request: ``Pull request [#7](url) by `login`: title``."""
    from specivo.services.repository_hooks import md_text

    note = pr_label(provider, pr)
    if pr.author_login:
        note += f" by `{pr.author_login.replace('`', '')}`"
    title = pr.title if len(pr.title) <= 200 else pr.title[:199] + "…"
    return note + (f": {md_text(title)}" if title else "")


def _values(event: PullRequestEvent) -> dict[str, Any]:
    values: dict[str, Any] = {
        "title": event.title,
        "url": event.url,
        "state": event.state,
        "source_branch": event.source_branch,
        "target_branch": event.target_branch,
        "author_login": event.author_login,
        "merged_by_login": event.merged_by_login,
        "merged_at": event.merged_at,
        "merge_commit_sha": event.merge_commit_sha,
        "provider_updated_at": event.updated_at,
    }
    for name, limit in _FIELD_MAX.items():
        if isinstance(values[name], str):
            values[name] = values[name][:limit]
    return values


async def _upsert(
    session: AsyncSession, integration: RepositoryIntegration, event: PullRequestEvent
) -> tuple[PullRequest, bool, str | None]:
    """The stored request, locked; whether *event* was applied to it; its state before (None when new)."""
    values = _values(event)
    inserted = (
        await session.execute(
            pg_insert(PullRequest)
            .values(integration_id=integration.id, number=event.number, **values)
            .on_conflict_do_nothing(constraint="uq_pull_requests_integration_number")
            .returning(PullRequest.id)
        )
    ).scalar_one_or_none()
    stmt = (
        select(PullRequest)
        .where(PullRequest.integration_id == integration.id, PullRequest.number == event.number)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    pr = (await session.execute(stmt)).scalar_one()
    if inserted is not None:
        return pr, True, None
    previous = pr.state
    if not is_newer(pr.provider_updated_at, pr.state, event.updated_at, event.state):
        return pr, False, previous
    for name, value in values.items():
        # GitLab names the author only on the event that opened the request.
        if value is None and name in ("author_login", "merged_by_login", "merged_at", "merge_commit_sha"):
            continue
        setattr(pr, name, value)
    await session.flush()
    return pr, True, previous


async def _issues(session: AsyncSession, links: dict[str, PullRequestLink], scope: dict[str, int]) -> dict[str, Issue]:
    pairs = {(link.key, link.number) for link in links.values()}
    rows = await session.execute(
        select(Issue)
        .where(tuple_(Issue.project_key, Issue.sequence_number).in_(pairs), Issue.project_id.in_(set(scope.values())))
        .order_by(Issue.id)
        .with_for_update()
    )
    return {issue.display_key: issue for issue in rows.scalars().all()}


async def process_pull_request(session: AsyncSession, integration: RepositoryIntegration, payload: dict):
    """Process one accepted pull request delivery; returns a ``ProcessResult``."""
    from specivo.services.repository_hooks import (
        ProcessResult,
        append_metadata,
        get_git_user,
        scope_project_keys,
    )

    event = get_provider(integration.provider).parse_pull_request(payload)
    if event is None:
        return ProcessResult(DELIVERY_IGNORED, "The payload names no pull request.")
    if event.default_branch and event.default_branch != integration.default_branch:
        integration.default_branch = event.default_branch[:255]
    label = f"Pull request #{event.number}" if integration.provider != "gitlab" else f"Merge request !{event.number}"

    scope = await scope_project_keys(session, integration)
    links = pull_request_links(event, frozenset(scope))
    exists = await session.scalar(
        select(PullRequest.id).where(PullRequest.integration_id == integration.id, PullRequest.number == event.number)
    )
    issues = await _issues(session, links, scope) if links else {}
    if exists is None and not issues and event.state != PR_MERGED:
        return ProcessResult(DELIVERY_PROCESSED, f"{label} ({event.state}) references no issue.")

    pr, applied, previous_state = await _upsert(session, integration, event)
    if not applied:
        return ProcessResult(DELIVERY_PROCESSED, f"{label}: the event is older than the stored state; nothing changed.")

    existing = {
        link.issue_id: link
        for link in (
            await session.execute(select(IssuePullRequestLink).where(IssuePullRequestLink.pull_request_id == pr.id))
        ).scalars()
    }
    git_user = await get_git_user(session)
    from specivo.services.journal_service import JournalService

    journals = JournalService()
    merged_before = previous_state == PR_MERGED
    new_count = 0
    for display_key, issue in issues.items():
        link = links[display_key]
        current = existing.get(issue.id)
        if current is not None:
            if link.closing and not current.closing:
                current.closing = True
            continue
        current = IssuePullRequestLink(
            pull_request_id=pr.id, issue_id=issue.id, source=link.source, closing=link.closing
        )
        session.add(current)
        existing[issue.id] = current
        new_count += 1
        await journals.add_comment(session, issue, git_user, pr_journal_note(integration.provider, pr))
        additions = {PULL_REQUESTS_METADATA_KEY: [pr.url or label]}
        if pr.source_branch:
            additions[BRANCHES_METADATA_KEY] = [pr.source_branch]
        append_metadata(issue, additions)
    if not merged_before:
        # Before the merge, closing follows the text as it is now: a dropped
        # reference or keyword no longer counts.
        closing_now = {issue.id: links[key].closing for key, issue in issues.items()}
        for issue_id, current in existing.items():
            current.closing = closing_now.get(issue_id, False)
        merging = (previous_state is not None and pr.state == PR_MERGED) or (
            previous_state is None and is_merge_event(event)
        )
        if merging:
            # The text at the merge decides what automation may act on; later edits never do.
            now = utcnow()
            for current in existing.values():
                if current.closing:
                    current.merge_qualified_at = now
    await session.flush()

    outcomes: list[tuple[str, str, str]] = []
    if commands.merge_applies(integration, pr):
        outcomes = await _merge_automation(session, integration, pr, existing, git_user, set(scope.values()))

    detail = f"{label} {pr.state}: {len(existing)} linked issue(s), {new_count} new."
    if outcomes:
        detail += " Merge automation: " + commands.describe(outcomes) + "."
    return ProcessResult(DELIVERY_PROCESSED, detail)


async def _merge_automation(
    session: AsyncSession,
    integration: RepositoryIntegration,
    pr: PullRequest,
    links: dict[int, IssuePullRequestLink],
    git_user,
    scope_project_ids: set[int],
) -> list[tuple[str, str, str]]:
    pending = [
        link
        for link in links.values()
        if link.closing and link.merge_qualified_at is not None and link.merge_handled_at is None
    ]
    if not pending:
        return []
    issues = {
        issue.id: issue
        for issue in (
            await session.execute(
                select(Issue)
                .where(Issue.id.in_([link.issue_id for link in pending]))
                .order_by(Issue.id)
                .with_for_update()
            )
        ).scalars()
    }
    label = pr_label(integration.provider, pr)
    outcomes: list[tuple[str, str, str]] = []
    for link in sorted(pending, key=lambda item: item.issue_id):
        issue = issues.get(link.issue_id)
        if issue is None:  # pragma: no cover - deleted while processing
            continue
        if issue.project_id not in scope_project_ids:
            # The scope was narrowed since the link was made; left unhandled.
            outcomes.append((issue.display_key, "merge", "out_of_scope"))
            continue
        outcome = await commands.run_merge_automation(
            session, integration, pr, issue, git_user, pr_label=label, note=link.merge_refused_at is None
        )
        # A refusal is retried by a later delivery of the merge (after an
        # administrator grants the transition); anything final is not. The
        # issue gets its "could not move" note once, not on every retry.
        if outcome in (commands.MOVED, "already_closed", "already_in_status"):
            link.merge_handled_at = utcnow()
        elif commands.is_refusal(outcome) and link.merge_refused_at is None:
            link.merge_refused_at = utcnow()
        outcomes.append((issue.display_key, "merge", outcome))
    await session.flush()
    return outcomes
