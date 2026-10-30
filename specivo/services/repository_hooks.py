"""Incoming repository webhooks: record deliveries and link pushed commits to issues.

The endpoint (``specivo.hooks.router``) checks size, integration, rate limit
and signature, then calls :func:`record_delivery`, which inserts the delivery
with ``ON CONFLICT DO NOTHING``: a delivery id seen before is a duplicate and
changes nothing. A push is stored ``accepted`` and processed after commit by
the Celery task ``process_repository_delivery``, which calls
:func:`process_delivery`:

1. Parse the push with the provider adapter and refresh the default branch.
2. Collect the issue references in each commit message and in the branch
   name, limited to the project keys in the integration's scope.
3. Upsert the commits that reference something (``repository_commits``),
   with ``matched_user_id`` set only from a *verified* email.
4. Insert the (commit, issue) links with ``ON CONFLICT DO NOTHING``. Only the
   newly inserted ones get a journal, written by ``$git``, and the commit SHA
   appended to the issue's ``commits`` metadata (no duplicates).
5. Mark the delivery ``processed``, ``ignored`` or ``failed`` with a reason.

The delivery detail never names an issue or a project outside the scope:
references to them are not references at all (see ``specivo.hooks.refs``).
A sweeper re-queues ``accepted`` rows that were never processed (broker down
at commit time); processing is idempotent, so a row handled twice is harmless.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from urllib.parse import quote

from sqlalchemy import delete, func, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.constants import (
    REPOSITORY_COMMIT_BATCH,
    REPOSITORY_COMMIT_MESSAGE_MAX_CHARS,
    REPOSITORY_DELIVERY_RETENTION_DAYS,
    REPOSITORY_MAX_REFS_PER_COMMIT,
    REPOSITORY_SWEEP_AGE_SECONDS,
    REPOSITORY_SWEEP_BATCH,
)
from specivo.core.utils import utcnow
from specivo.hooks.providers import CommitData, PushEvent, get_provider
from specivo.hooks.refs import Ref, extract_branch_refs, extract_refs
from specivo.models.issue import Issue
from specivo.models.project import Project
from specivo.models.repository import (
    DELIVERY_ACCEPTED,
    DELIVERY_FAILED,
    DELIVERY_IGNORED,
    DELIVERY_PROCESSED,
    SCOPE_PROJECT_AND_SUBPROJECTS,
    IssueCommitLink,
    RepositoryCommit,
    RepositoryDelivery,
    RepositoryIntegration,
)
from specivo.models.user import User
from specivo.services.anonymous_user_service import real_users_clause

logger = logging.getLogger(__name__)

#: Login of the service account that writes commit journals.
GIT_LOGIN = "$git"
#: The issue metadata key commit SHAs are appended to.
COMMITS_METADATA_KEY = "commits"

_EVENT_MAX_LENGTH = 50
_DETAIL_MAX_LENGTH = 1000
_FIRST_LINE_MAX_CHARS = 200
# Per-commit lists that can be large and are not needed afterwards.
_PRUNED_COMMIT_KEYS = ("added", "removed", "modified")


async def get_git_user(session: AsyncSession) -> User:
    """The ``$git`` service account; RuntimeError when the migration that creates it has not run."""
    user = (await session.execute(select(User).where(User.login == GIT_LOGIN))).scalar_one_or_none()
    if user is None:
        raise RuntimeError("The $git service account is missing; run the database migrations")
    return user


# ---------------------------------------------------------------------------
# Receiving
# ---------------------------------------------------------------------------


def prune_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """The payload without per-commit file lists, as stored on the delivery."""
    pruned = dict(payload)
    for key in ("commits",):
        if isinstance(pruned.get(key), list):
            pruned[key] = [_prune_commit(c) for c in pruned[key]]
    if isinstance(pruned.get("head_commit"), dict):
        pruned["head_commit"] = _prune_commit(pruned["head_commit"])
    return pruned


def _prune_commit(commit: Any) -> Any:
    if not isinstance(commit, dict):
        return commit
    return {k: v for k, v in commit.items() if k not in _PRUNED_COMMIT_KEYS}


def _truncate(value: str | None, limit: int) -> str | None:
    return None if value is None else value[:limit]


async def find_integration(session: AsyncSession, provider: str, public_id: Any) -> RepositoryIntegration | None:
    """The active integration for this hook URL, or None."""
    stmt = select(RepositoryIntegration).where(
        RepositoryIntegration.public_id == public_id,
        RepositoryIntegration.provider == provider,
        RepositoryIntegration.is_active.is_(True),
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def record_delivery(
    session: AsyncSession,
    integration: RepositoryIntegration,
    *,
    delivery_id: str,
    event: str,
    status: str,
    payload: dict[str, Any] | None,
    detail: str | None = None,
) -> int | None:
    """Insert the delivery; returns its row id, or None when the delivery id was seen before.

    A redelivery of a delivery whose processing failed is accepted again.

    Also marks the integration as having received a delivery now.
    """
    now = utcnow()
    stmt = (
        pg_insert(RepositoryDelivery)
        .values(
            integration_id=integration.id,
            delivery_id=delivery_id,
            event=event[:_EVENT_MAX_LENGTH],
            status=status,
            detail=_truncate(detail, _DETAIL_MAX_LENGTH),
            payload=None if payload is None else prune_payload(payload),
            received_at=now,
            processed_at=None if status == DELIVERY_ACCEPTED else now,
        )
        .on_conflict_do_nothing(constraint="uq_repository_deliveries_integration_delivery")
        .returning(RepositoryDelivery.id)
    )
    row_id = (await session.execute(stmt)).scalar_one_or_none()
    if row_id is None and status == DELIVERY_ACCEPTED:
        # A forge redelivers with the same id. A delivery whose processing
        # failed (a deadlock, a lost connection) gets another go instead of
        # being answered as a duplicate for ever.
        row_id = (
            await session.execute(
                update(RepositoryDelivery)
                .where(
                    RepositoryDelivery.integration_id == integration.id,
                    RepositoryDelivery.delivery_id == delivery_id,
                    RepositoryDelivery.status == DELIVERY_FAILED,
                )
                .values(status=DELIVERY_ACCEPTED, detail=None, processed_at=None, received_at=now)
                .returning(RepositoryDelivery.id)
                .execution_options(synchronize_session=False)
            )
        ).scalar_one_or_none()
    if row_id is not None:
        await session.execute(
            update(RepositoryIntegration)
            .where(RepositoryIntegration.id == integration.id)
            .values(last_delivery_at=now)
            .execution_options(synchronize_session=False)
        )
    return row_id


def queue_processing(delivery_ids: Iterable[int]) -> int:
    """Queue ``process_repository_delivery`` for each row; never raises. Returns how many were queued.

    A row that could not be queued stays ``accepted`` and the sweeper queues it later.
    """
    from specivo.tasks.repositories import process_repository_delivery

    queued = 0
    for delivery_id in delivery_ids:
        try:
            process_repository_delivery.apply_async(args=[delivery_id], retry=False)
        except Exception as exc:  # noqa: BLE001 - a dead broker must not fail the forge's request
            logger.warning(
                "Could not queue repository delivery %d (%s); the sweeper will retry it",
                delivery_id,
                type(exc).__name__,
            )
            continue
        queued += 1
    return queued


# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------


@dataclass
class ProcessResult:
    """What processing a delivery did; ``detail`` goes to the delivery log."""

    status: str
    detail: str
    new_links: list[tuple[int, int]] = field(default_factory=list)


async def process_delivery(session: AsyncSession, delivery_row_id: int) -> ProcessResult | None:
    """Process one accepted delivery. Returns None when there is nothing to do.

    Takes the row with ``FOR UPDATE SKIP LOCKED``, so two tasks for the same row
    (a retry next to the sweeper) do not both work on it. The caller commits.
    When processing raises, the session is rolled back and only the failure
    is recorded.
    """
    stmt = (
        select(RepositoryDelivery)
        .where(RepositoryDelivery.id == delivery_row_id, RepositoryDelivery.status == DELIVERY_ACCEPTED)
        .with_for_update(skip_locked=True)
    )
    delivery = (await session.execute(stmt)).scalar_one_or_none()
    if delivery is None:
        return None
    integration = await session.get(RepositoryIntegration, delivery.integration_id)
    if integration is None:  # pragma: no cover - the delivery is deleted with it
        return None

    delivery_pk, forge_delivery_id, integration_id = delivery.id, delivery.delivery_id, integration.id
    if not integration.is_active:
        result = ProcessResult(DELIVERY_IGNORED, "The integration is disabled.")
    else:
        try:
            result = await _process_push(session, integration, delivery.payload or {})
        except Exception as exc:  # noqa: BLE001 - recorded on the delivery, never lost
            logger.exception(
                "Repository delivery %d failed",
                delivery_pk,
                extra={"integration_id": integration_id, "delivery_id": forge_delivery_id},
            )
            # Undo the partial work, then record the failure on its own.
            await session.rollback()
            result = ProcessResult(DELIVERY_FAILED, f"Processing failed: {type(exc).__name__}")
            await session.execute(
                update(RepositoryDelivery)
                .where(RepositoryDelivery.id == delivery_pk, RepositoryDelivery.status == DELIVERY_ACCEPTED)
                .values(status=result.status, detail=result.detail, processed_at=utcnow())
            )
            return result

    delivery.status = result.status
    delivery.detail = _truncate(result.detail, _DETAIL_MAX_LENGTH)
    delivery.processed_at = utcnow()
    await session.flush()
    logger.info(
        "Repository delivery %d %s: %s",
        delivery_pk,
        result.status,
        result.detail,
        extra={
            "integration_id": integration_id,
            "delivery_id": forge_delivery_id,
            "event": delivery.event,
            "outcome": result.status,
        },
    )
    return result


async def scope_project_keys(session: AsyncSession, integration: RepositoryIntegration) -> dict[str, int]:
    """Project key → project id for the projects references may resolve to."""
    root = await session.get(Project, integration.project_id)
    if root is None:  # pragma: no cover - the integration is deleted with its project
        return {}
    if integration.scope != SCOPE_PROJECT_AND_SUBPROJECTS:
        return {root.key: root.id}
    rows = await session.execute(
        select(Project.key, Project.id).where(
            or_(Project.id == root.id, Project.path.startswith(f"{root.path}.", autoescape=True))
        )
    )
    scope = {key: project_id for key, project_id in rows.all()}
    # A subproject counts only while the person who connected the repository
    # may manage it, so a subproject created later, or one they never managed,
    # never receives journals from a repository they chose.
    creator = await session.get(User, integration.created_by_id) if integration.created_by_id else None
    if creator is not None and creator.is_admin:
        return scope
    from specivo.services.permission_service import Permission, check_permission

    allowed = {root.key: root.id}
    if creator is not None:
        for key, project_id in scope.items():
            if project_id != root.id and await check_permission(
                creator, project_id, Permission.MANAGE_PROJECT, session
            ):
                allowed[key] = project_id
    return allowed


def _commit_refs(
    commit: CommitData, branch_refs: Sequence[Ref], known_keys: frozenset[str]
) -> dict[str, tuple[Ref, str]]:
    """display key → (ref, source); a reference in the message wins over the branch name."""
    refs: dict[str, tuple[Ref, str]] = {r.display_key: (r, "branch") for r in branch_refs}
    # Parse only what is stored: the message is capped before the parser sees it.
    for ref in extract_refs(commit.message[:REPOSITORY_COMMIT_MESSAGE_MAX_CHARS], known_keys):
        refs[ref.display_key] = (ref, "message")
    return dict(list(refs.items())[:REPOSITORY_MAX_REFS_PER_COMMIT])


async def _matched_users(session: AsyncSession, emails: Iterable[str]) -> dict[str, int]:
    """Lower-cased email → user id, for real accounts whose email is verified."""
    wanted = {e.strip().lower() for e in emails if e and e.strip()}
    if not wanted:
        return {}
    rows = await session.execute(
        select(func.lower(User.email), User.id).where(
            func.lower(User.email).in_(wanted),
            User.email_verified_at.is_not(None),
            real_users_clause(),
        )
    )
    return {email: user_id for email, user_id in rows.all()}


async def _process_push(
    session: AsyncSession, integration: RepositoryIntegration, payload: dict[str, Any]
) -> ProcessResult:
    push = get_provider(integration.provider).parse_push(payload)
    if push.default_branch and push.default_branch != integration.default_branch:
        integration.default_branch = push.default_branch[:255]
    if push.branch is None:
        return ProcessResult(DELIVERY_IGNORED, f"Not a branch push ({push.ref or 'no ref'}).")
    if not push.commits:
        return ProcessResult(DELIVERY_PROCESSED, "No commits.")

    scope = await scope_project_keys(session, integration)
    known_keys = frozenset(scope)
    branch_refs = extract_branch_refs(push.branch, known_keys)
    referencing = [(c, refs) for c in push.commits if (refs := _commit_refs(c, branch_refs, known_keys))]
    if not referencing:
        return ProcessResult(DELIVERY_PROCESSED, f"{len(push.commits)} commit(s), no issue references.")

    new_links: list[tuple[int, int]] = []
    for start in range(0, len(referencing), REPOSITORY_COMMIT_BATCH):
        batch = referencing[start : start + REPOSITORY_COMMIT_BATCH]
        new_links.extend(await _link_batch(session, integration, push, batch, scope))

    linked_issues = len({issue_id for _commit_id, issue_id in new_links})
    detail = (
        f"{len(push.commits)} commit(s), {len(referencing)} with references; "
        f"{len(new_links)} new link(s) to {linked_issues} issue(s)."
    )
    return ProcessResult(DELIVERY_PROCESSED, detail, new_links)


async def _link_batch(
    session: AsyncSession,
    integration: RepositoryIntegration,
    push: PushEvent,
    batch: Sequence[tuple[CommitData, dict[str, tuple[Ref, str]]]],
    scope: dict[str, int],
) -> list[tuple[int, int]]:
    """Store one batch of referencing commits and their links; journal the new links."""
    # Resolve references to issues in scope. A reference to a missing issue is dropped silently.
    pairs = {(ref.key, ref.number) for _c, refs in batch for ref, _s in refs.values()}
    issue_rows = await session.execute(
        select(Issue.id, Issue.project_key, Issue.sequence_number).where(
            tuple_(Issue.project_key, Issue.sequence_number).in_(pairs),
            Issue.project_id.in_(set(scope.values())),
        )
    )
    issue_ids = {f"{key}-{number}": issue_id for issue_id, key, number in issue_rows.all()}
    if not issue_ids:
        return []

    batch = [(c, refs) for c, refs in batch if any(k in issue_ids for k in refs)]
    users = await _matched_users(session, (c.author_email for c, _refs in batch))
    await session.execute(
        pg_insert(RepositoryCommit)
        .values(
            [
                {
                    "integration_id": integration.id,
                    "sha": c.sha,
                    "message": c.message[:REPOSITORY_COMMIT_MESSAGE_MAX_CHARS],
                    "url": c.url[:2048],
                    "author_name": c.author_name[:255],
                    "author_email": c.author_email[:255],
                    "matched_user_id": users.get(c.author_email.strip().lower()),
                    "committed_at": c.timestamp,
                    "branch": push.branch[:255] if push.branch else None,
                }
                for c, _refs in batch
            ]
        )
        .on_conflict_do_nothing(constraint="uq_repository_commits_integration_sha")
    )
    commit_rows = (
        (
            await session.execute(
                select(RepositoryCommit).where(
                    RepositoryCommit.integration_id == integration.id,
                    RepositoryCommit.sha.in_([c.sha for c, _refs in batch]),
                )
            )
        )
        .scalars()
        .all()
    )
    commits = {row.sha: row for row in commit_rows}

    link_values = [
        {
            "commit_id": commits[c.sha].id,
            "issue_id": issue_ids[display_key],
            "source": source,
            "keyword": ref.keyword if source == "message" else None,
        }
        for c, refs in batch
        for display_key, (ref, source) in refs.items()
        if display_key in issue_ids
    ]
    inserted = await session.execute(
        pg_insert(IssueCommitLink)
        .values(link_values)
        .on_conflict_do_nothing()
        .returning(IssueCommitLink.commit_id, IssueCommitLink.issue_id)
    )
    new_links = [(commit_id, issue_id) for commit_id, issue_id in inserted.all()]
    if new_links:
        by_id = {row.id: row for row in commit_rows}
        await _record_new_links(session, [(by_id[c], i) for c, i in new_links])
    return new_links


async def _record_new_links(session: AsyncSession, links: Sequence[tuple[RepositoryCommit, int]]) -> None:
    """One ``$git`` journal per new (commit, issue) link, and the SHA appended to the issue's metadata."""
    from specivo.services.journal_service import JournalService

    git_user = await get_git_user(session)
    journals = JournalService()
    issue_ids = sorted({issue_id for _commit, issue_id in links})
    # Lock the issues in id order, so two pushes touching the same issues cannot deadlock.
    issues = {
        issue.id: issue
        for issue in (
            await session.execute(select(Issue).where(Issue.id.in_(issue_ids)).order_by(Issue.id).with_for_update())
        )
        .scalars()
        .all()
    }
    shas_by_issue: dict[int, list[str]] = {}
    for commit, issue_id in links:
        issue = issues.get(issue_id)
        if issue is None:  # pragma: no cover - deleted between the link insert and now
            continue
        await journals.add_comment(session, issue, git_user, commit_journal_note(commit))
        shas_by_issue.setdefault(issue_id, []).append(commit.sha)
    for issue_id, shas in shas_by_issue.items():
        issue = issues[issue_id]
        metadata = dict(issue.issue_metadata or {})
        merged = append_unique(metadata.get(COMMITS_METADATA_KEY), shas)
        if merged is not None:
            metadata[COMMITS_METADATA_KEY] = merged
            issue.issue_metadata = metadata
    await session.flush()


def append_unique(existing: Any, values: Iterable[str]) -> list[Any] | None:
    """*existing* (a list, or nothing) with *values* appended, skipping ones already there.

    None when *existing* is set but is not a list: the key holds something
    else, and it is left alone rather than overwritten.
    """
    if existing is None:
        existing = []
    elif not isinstance(existing, list):
        return None
    merged = list(existing)
    for value in values:
        if value not in merged:
            merged.append(value)
    return merged


_MARKDOWN_SPECIAL = str.maketrans({c: "\\" + c for c in "\\`*_{}[]<>()#+!|~"})


def _md_text(value: str) -> str:
    """Plain text for a Markdown note: no links, emphasis or HTML from a commit author."""
    return value.translate(_MARKDOWN_SPECIAL)


def _safe_url(url: str) -> str | None:
    if not url.startswith(("https://", "http://")):
        return None
    return quote(url, safe=":/?&=#%@+,;~-._!$'*")


def commit_journal_note(commit: RepositoryCommit) -> str:
    """The journal text for a newly linked commit.

    ``Commit [`abc1234`](url) on `branch` by Author Name: first line``
    """
    short = f"`{commit.short_sha}`"
    url = _safe_url(commit.url)
    parts = [f"Commit [{short}]({url})" if url else f"Commit {short}"]
    if commit.branch:
        parts.append(f"on `{commit.branch.replace('`', '')}`")
    if commit.author_name:
        parts.append(f"by {_md_text(commit.author_name)}")
    first_line = commit.first_line
    if len(first_line) > _FIRST_LINE_MAX_CHARS:
        first_line = first_line[: _FIRST_LINE_MAX_CHARS - 1] + "…"
    return " ".join(parts) + (f": {_md_text(first_line)}" if first_line else "")


# ---------------------------------------------------------------------------
# Sweeping and retention
# ---------------------------------------------------------------------------


async def claim_stale(session: AsyncSession) -> list[int]:
    """Ids of accepted deliveries old enough that their task was probably lost."""
    cutoff = utcnow() - timedelta(seconds=REPOSITORY_SWEEP_AGE_SECONDS)
    rows = await session.execute(
        select(RepositoryDelivery.id)
        .where(RepositoryDelivery.status == DELIVERY_ACCEPTED, RepositoryDelivery.received_at < cutoff)
        .order_by(RepositoryDelivery.received_at)
        .limit(REPOSITORY_SWEEP_BATCH)
    )
    return list(rows.scalars().all())


async def purge_old_deliveries(session: AsyncSession) -> int:
    """Delete delivery log rows past the retention period; returns how many."""
    cutoff = utcnow() - timedelta(days=REPOSITORY_DELIVERY_RETENTION_DAYS)
    result = await session.execute(delete(RepositoryDelivery).where(RepositoryDelivery.received_at < cutoff))
    return int(getattr(result, "rowcount", 0) or 0)
