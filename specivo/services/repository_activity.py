"""Development activity beyond commits and pull requests: CI, deployments, tags and releases, reviews.

Called by :func:`specivo.services.repository_hooks.process_delivery` for an
accepted delivery, by the kind its provider gives the event
(``ForgeProvider.event_kind``). The caller commits. Specivo never calls the
forge: everything here comes from webhook payloads.

How activity reaches an issue
-----------------------------
- **CI runs and deployments** have no link table. They belong to an issue,
  when read, through what is linked to it in the same repository:

  - their commit is a linked commit, or the merge commit of a linked pull
    request;
  - a CI run ran for a linked pull request (its number);
  - they ran on (CI) or deployed (deployment) a branch that is a linked pull
    request's source branch, or the branch of a commit linked by its branch
    name, other than the repository's default branch.

  So a commit or pull request detached from or moved to an issue takes its
  builds and deployments with it, and an event that arrives before the push
  that links its commit is still found.
- **Reviews** belong to their pull request, and so to its issues.
- **Releases** are linked when a tag or release event is processed
  (:func:`link_release`), see "Released in" below.

Out-of-order events
-------------------
One row per forge object holds its latest state; an event is applied only
when it is not older than the stored state (``specivo.hooks.development``):

- CI: (attempt, forge time, phase); a re-run is newer, and a late "running"
  never undoes a finished run of the same time.
- Deployments: (forge time, phase); ``deployed_at`` is set by the first
  success and kept when the deployment later becomes inactive.
- Reviews: per reviewer the newest wins; a plain comment does not withdraw an
  approval or a change request.
- Tags: a later event fills in what an earlier one did not say (the commit,
  the release), and never clears it, except a deleted release (``is_release``
  false) or a deleted tag (the row goes).

Caps
----
Per repository integration, CI runs, deployments and tags are capped
(``DEVELOPMENT_*_PER_REPOSITORY``): when a new row passes the cap, the oldest
by the forge's time are deleted. Per issue, release links are capped
(``DEVELOPMENT_RELEASES_PER_ISSUE``, oldest deleted), and reads consider the
newest ``DEVELOPMENT_ACTIVITY_PER_ISSUE`` CI runs and deployments. Reviewers
past ``DEVELOPMENT_REVIEWERS_PER_PULL_REQUEST`` on one request are not
recorded.

"Released in"
-------------
A tag records the issues it contains, by three rules, each needing only
webhook data:

1. ``tag_commit``: the tag points at a linked commit, or at the merge commit
   of a linked pull request.
2. ``tag_push``: the push that created the tag listed a linked commit.
3. ``default_branch``: the tag points at a commit Specivo saw reach the
   default branch (a linked commit pushed to it, or the merge commit of a
   pull request merged into it); then every issue whose linked commit was
   pushed to the default branch, or whose pull request was merged into it,
   no later than that commit, and that is not yet released in another tag of
   the repository, is released in this one. This assumes the default branch
   only moves forward (no force push), as it does in normal use. A tag on
   another branch only gets rules 1 and 2.

When the issue's project has a version named like the tag (``v1.2`` matches
version ``v1.2`` or ``1.2``) or like the release, the link names it; versions
are never created or assigned.
"""

from __future__ import annotations

import heapq
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from fastapi import Request
from sqlalchemy import Select, delete, func, or_, select, tuple_, union_all
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core import constants as c
from specivo.core.exceptions import NotFoundError, ValidationError
from specivo.core.utils import utcnow
from specivo.hooks import development as dev
from specivo.hooks.development import CiRunEvent, DeploymentEvent, ReleaseEvent, ReviewEvent
from specivo.hooks.providers import PushEvent, get_provider
from specivo.models.issue import Issue
from specivo.models.repository import (
    DELIVERY_IGNORED,
    DELIVERY_PROCESSED,
    PR_MERGED,
    CiRun,
    Deployment,
    DevelopmentDetachment,
    IssueCommitLink,
    IssuePullRequestLink,
    IssueReleaseLink,
    PullRequest,
    PullRequestReview,
    RepositoryCommit,
    RepositoryIntegration,
    RepositoryRelease,
)
from specivo.models.user import User
from specivo.models.version import Version

if TYPE_CHECKING:
    from specivo.services.repository_hooks import ProcessResult

logger = logging.getLogger(__name__)


def _result(status: str, detail: str) -> ProcessResult:
    from specivo.services.repository_hooks import ProcessResult

    return ProcessResult(status, detail)


# ---------------------------------------------------------------------------
# Caps
# ---------------------------------------------------------------------------


async def _prune(session: AsyncSession, model: Any, integration_id: int, cap: int) -> int:
    """Delete the integration's oldest rows of *model* past *cap*; returns how many."""
    overflow = (
        select(model.id)
        .where(model.integration_id == integration_id)
        .order_by(model.occurred_at.desc(), model.id.desc())
        .offset(cap)
    )
    result = await session.execute(delete(model).where(model.id.in_(overflow.scalar_subquery())))
    return int(getattr(result, "rowcount", 0) or 0)


# ---------------------------------------------------------------------------
# CI
# ---------------------------------------------------------------------------


async def process_ci(
    session: AsyncSession, integration: RepositoryIntegration, event_name: str, payload: dict
) -> ProcessResult:
    """Store one CI event (a run, job, check or pipeline); returns a ``ProcessResult``."""
    event = get_provider(integration.provider).parse_ci(event_name, payload)
    if event is None:
        return _result(DELIVERY_IGNORED, f"The {event_name} payload names no run.")
    applied, inserted = await _upsert_ci(session, integration, event)
    label = f"{event.run_type.replace('_', ' ').capitalize()} {event.name!r} ({event.status})"
    if not applied:
        return _result(DELIVERY_PROCESSED, f"{label}: the event is older than the stored state; nothing changed.")
    pruned = await _prune(session, CiRun, integration.id, c.DEVELOPMENT_CI_RUNS_PER_REPOSITORY) if inserted else 0
    detail = f"{label} recorded."
    if pruned:
        detail += f" {pruned} oldest run(s) removed (cap {c.DEVELOPMENT_CI_RUNS_PER_REPOSITORY})."
    return _result(DELIVERY_PROCESSED, detail)


def _ci_values(event: CiRunEvent) -> dict[str, Any]:
    return {
        "name": event.name,
        "status": event.status,
        "head_sha": event.head_sha,
        "branch": event.branch,
        "pull_request_number": event.pull_request_numbers[0] if event.pull_request_numbers else None,
        "url": event.url,
        "attempt": event.attempt,
        "started_at": event.started_at,
        "completed_at": event.completed_at,
        "provider_updated_at": event.updated_at,
        "occurred_at": event.updated_at or utcnow(),
    }


async def _upsert_ci(session: AsyncSession, integration: RepositoryIntegration, event: CiRunEvent) -> tuple[bool, bool]:
    """(applied, inserted) for *event* against the stored run."""
    values = _ci_values(event)
    key = {
        "integration_id": integration.id,
        "provider": integration.provider,
        "run_type": event.run_type,
        "external_id": event.external_id,
    }
    inserted = (
        await session.execute(
            pg_insert(CiRun)
            .values(**key, **values)
            .on_conflict_do_nothing(constraint="uq_ci_runs_integration_provider_type_id")
            .returning(CiRun.id)
        )
    ).scalar_one_or_none()
    if inserted is not None:
        return True, True
    row = (
        await session.execute(
            select(CiRun).filter_by(**key).with_for_update().execution_options(populate_existing=True)
        )
    ).scalar_one()
    stored = dev.ci_order_key(row.attempt, row.provider_updated_at, row.status)
    incoming = dev.ci_order_key(event.attempt, event.updated_at, event.status)
    if incoming < stored:
        return False, False
    for name, value in values.items():
        # A later event that does not repeat a field keeps the stored one.
        if value is None and name in ("head_sha", "branch", "pull_request_number", "started_at"):
            continue
        setattr(row, name, value)
    await session.flush()
    return True, False


# ---------------------------------------------------------------------------
# Deployments
# ---------------------------------------------------------------------------


async def process_deployment(
    session: AsyncSession, integration: RepositoryIntegration, event_name: str, payload: dict
) -> ProcessResult:
    """Store one deployment or deployment status event; returns a ``ProcessResult``."""
    event = get_provider(integration.provider).parse_deployment(event_name, payload)
    if event is None:
        return _result(DELIVERY_IGNORED, f"The {event_name} payload names no deployment.")
    applied, inserted = await _upsert_deployment(session, integration, event)
    label = f"Deployment to {event.environment!r} ({event.status})"
    if not applied:
        return _result(DELIVERY_PROCESSED, f"{label}: the event is older than the stored state; nothing changed.")
    cap = c.DEVELOPMENT_DEPLOYMENTS_PER_REPOSITORY
    pruned = await _prune(session, Deployment, integration.id, cap) if inserted else 0
    detail = f"{label} recorded."
    if pruned:
        detail += f" {pruned} oldest deployment(s) removed (cap {cap})."
    return _result(DELIVERY_PROCESSED, detail)


async def _upsert_deployment(
    session: AsyncSession, integration: RepositoryIntegration, event: DeploymentEvent
) -> tuple[bool, bool]:
    now = utcnow()
    deployed_at = (event.updated_at or now) if event.status == dev.CI_SUCCESS else None
    values: dict[str, Any] = {
        "environment": event.environment,
        "status": event.status,
        "sha": event.sha,
        "ref": event.ref,
        "url": event.url,
        "provider_updated_at": event.updated_at,
        "occurred_at": event.updated_at or now,
    }
    key = {"integration_id": integration.id, "provider": integration.provider, "external_id": event.external_id}
    inserted = (
        await session.execute(
            pg_insert(Deployment)
            .values(**key, **values, deployed_at=deployed_at)
            .on_conflict_do_nothing(constraint="uq_deployments_integration_provider_id")
            .returning(Deployment.id)
        )
    ).scalar_one_or_none()
    if inserted is not None:
        return True, True
    row = (
        await session.execute(
            select(Deployment).filter_by(**key).with_for_update().execution_options(populate_existing=True)
        )
    ).scalar_one()
    if dev.deployment_order_key(event.updated_at, event.status) < dev.deployment_order_key(
        row.provider_updated_at, row.status
    ):
        # An older event still says when it succeeded, if the stored row does not know.
        if deployed_at is not None and row.deployed_at is None:
            row.deployed_at = deployed_at
            await session.flush()
        return False, False
    for name, value in values.items():
        if (value is None or value == "") and name in ("sha", "ref", "url"):
            continue
        setattr(row, name, value)
    if deployed_at is not None and row.deployed_at is None:
        row.deployed_at = deployed_at
    await session.flush()
    return True, False


# ---------------------------------------------------------------------------
# Reviews
# ---------------------------------------------------------------------------


async def apply_reviews(
    session: AsyncSession, integration: RepositoryIntegration, reviews: Sequence[ReviewEvent]
) -> tuple[int, str | None]:
    """Record reviewer verdicts on stored pull requests; (how many changed, a reason when none could be)."""
    if not reviews:
        return 0, None
    numbers = {r.number for r in reviews}
    prs = {
        pr.number: pr
        for pr in (
            await session.execute(
                select(PullRequest).where(
                    PullRequest.integration_id == integration.id,
                    PullRequest.provider == integration.provider,
                    PullRequest.number.in_(numbers),
                )
            )
        ).scalars()
    }
    if not prs:
        return 0, "the pull request references no issue (not stored)"
    changed = 0
    now = utcnow()
    for review in reviews:
        pr = prs.get(review.number)
        if pr is None:
            continue
        row = (
            await session.execute(
                select(PullRequestReview)
                .where(
                    PullRequestReview.pull_request_id == pr.id,
                    PullRequestReview.reviewer_login == review.reviewer_login,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if review.state is None:
            # A withdrawn approval (GitLab): applies unless the stored review is newer.
            if row is not None and not (
                row.provider_submitted_at and review.submitted_at and review.submitted_at < row.provider_submitted_at
            ):
                await session.delete(row)
                changed += 1
            continue
        if row is None:
            count = await session.scalar(
                select(func.count()).select_from(PullRequestReview).where(PullRequestReview.pull_request_id == pr.id)
            )
            if (count or 0) >= c.DEVELOPMENT_REVIEWERS_PER_PULL_REQUEST:
                continue
            inserted = (
                await session.execute(
                    pg_insert(PullRequestReview)
                    .values(
                        pull_request_id=pr.id,
                        reviewer_login=review.reviewer_login,
                        state=review.state,
                        url=review.url,
                        submitted_at=review.submitted_at or now,
                        provider_submitted_at=review.submitted_at,
                    )
                    .on_conflict_do_nothing(constraint="uq_pull_request_reviews_pr_reviewer")
                    .returning(PullRequestReview.id)
                )
            ).scalar_one_or_none()
            changed += inserted is not None
            continue
        if dev.review_replaces(row.state, row.provider_submitted_at, review.state, review.submitted_at):
            row.state = review.state
            row.url = review.url or row.url
            row.submitted_at = review.submitted_at or now
            row.provider_submitted_at = review.submitted_at
            changed += 1
    await session.flush()
    return changed, None


async def process_review(
    session: AsyncSession, integration: RepositoryIntegration, event_name: str, payload: dict
) -> ProcessResult:
    """A review event (GitHub ``pull_request_review``, Gitea/Forgejo review events)."""
    reviews = get_provider(integration.provider).parse_reviews(event_name, payload)
    if not reviews:
        return _result(DELIVERY_IGNORED, "The payload names no review.")
    changed, reason = await apply_reviews(session, integration, reviews)
    if reason:
        return _result(DELIVERY_PROCESSED, f"Review not recorded: {reason}.")
    return _result(DELIVERY_PROCESSED, f"{len(reviews)} review(s), {changed} recorded.")


# ---------------------------------------------------------------------------
# Tags and releases
# ---------------------------------------------------------------------------


async def process_release(
    session: AsyncSession, integration: RepositoryIntegration, event_name: str, payload: dict
) -> ProcessResult:
    """A ``create`` (tag) or ``release`` event; returns a ``ProcessResult``."""
    event = get_provider(integration.provider).parse_release(event_name, payload)
    if event is None:
        return _result(DELIVERY_IGNORED, f"The {event_name} event is not about a tag.")
    return await record_release(session, integration, event)


async def process_tag_push(session: AsyncSession, integration: RepositoryIntegration, push: PushEvent) -> ProcessResult:
    """A push of ``refs/tags/...``: create, move or delete the tag."""
    tag = push.tag or ""
    event = ReleaseEvent(
        tag_name=tag[:255],
        name="",
        sha=push.after,
        url="",
        released_at=None,
        is_release=False,
        deleted=push.deleted,
        commit_shas=tuple(commit.sha for commit in push.commits),
    )
    return await record_release(session, integration, event, from_push=True)


async def record_release(
    session: AsyncSession, integration: RepositoryIntegration, event: ReleaseEvent, *, from_push: bool = False
) -> ProcessResult:
    """Store a tag or release and link the issues it contains."""
    key = {"integration_id": integration.id, "provider": integration.provider, "tag_name": event.tag_name}
    if event.deleted:
        result = await session.execute(delete(RepositoryRelease).filter_by(**key))
        gone = int(getattr(result, "rowcount", 0) or 0)
        return _result(DELIVERY_PROCESSED, f"Tag {event.tag_name} deleted." if gone else "Unknown tag deleted.")
    now = utcnow()
    inserted = (
        await session.execute(
            pg_insert(RepositoryRelease)
            .values(
                **key,
                name=event.name,
                sha=event.sha,
                url=event.url,
                is_release=event.is_release,
                released_at=event.released_at if event.is_release else None,
                occurred_at=event.released_at or now,
            )
            .on_conflict_do_nothing(constraint="uq_repository_releases_integration_tag")
            .returning(RepositoryRelease.id)
        )
    ).scalar_one_or_none()
    release = (
        await session.execute(
            select(RepositoryRelease).filter_by(**key).with_for_update().execution_options(populate_existing=True)
        )
    ).scalar_one()
    if inserted is None:
        if event.sha and (from_push or release.sha is None):
            # A push moves the tag; other events only fill in a commit no one gave yet.
            release.sha = event.sha
        if event.is_release:
            release.is_release = True
            release.name = event.name or release.name
            release.url = event.url or release.url
            release.released_at = event.released_at or release.released_at
            release.occurred_at = event.released_at or release.occurred_at
        elif event.release_deleted:
            release.is_release = False
        await session.flush()
    pruned = 0
    if inserted is not None:
        pruned = await _prune(session, RepositoryRelease, integration.id, c.DEVELOPMENT_RELEASES_PER_REPOSITORY)
    linked = await link_release(session, integration, release, event.commit_shas)
    kind = "Release" if release.is_release else "Tag"
    detail = f"{kind} {release.tag_name}: {linked} issue(s) newly released in it."
    if pruned:
        detail += f" {pruned} oldest tag(s) removed (cap {c.DEVELOPMENT_RELEASES_PER_REPOSITORY})."
    return _result(DELIVERY_PROCESSED, detail)


async def _default_branch_time(session: AsyncSession, integration: RepositoryIntegration, sha: str) -> datetime | None:
    """When the commit *sha* reached the default branch, as Specivo saw it; None when it did not see it."""
    commit_time = await session.scalar(
        select(RepositoryCommit.created_at).where(
            RepositoryCommit.integration_id == integration.id,
            RepositoryCommit.sha == sha,
            RepositoryCommit.branch == integration.default_branch,
        )
    )
    merge_time = await session.scalar(
        select(func.coalesce(PullRequest.merged_at, PullRequest.provider_updated_at, PullRequest.updated_at)).where(
            PullRequest.integration_id == integration.id,
            PullRequest.provider == integration.provider,
            PullRequest.merge_commit_sha == sha,
            PullRequest.state == PR_MERGED,
            PullRequest.target_branch == integration.default_branch,
        )
    )
    return dev.latest_time(commit_time, merge_time)


async def link_release(
    session: AsyncSession, integration: RepositoryIntegration, release: RepositoryRelease, push_shas: Iterable[str]
) -> int:
    """Link the issues *release* contains (see the module docstring); returns how many links are new."""
    found: dict[int, str] = {}
    shas = {release.sha} if release.sha else set()
    for source, wanted in (("tag_commit", shas), ("tag_push", set(push_shas) - shas)):
        if not wanted:
            continue
        rows = await session.execute(
            select(IssueCommitLink.issue_id)
            .join(RepositoryCommit, RepositoryCommit.id == IssueCommitLink.commit_id)
            .where(RepositoryCommit.integration_id == integration.id, RepositoryCommit.sha.in_(wanted))
            .union(
                select(IssuePullRequestLink.issue_id)
                .join(PullRequest, PullRequest.id == IssuePullRequestLink.pull_request_id)
                .where(
                    PullRequest.integration_id == integration.id,
                    PullRequest.provider == integration.provider,
                    PullRequest.merge_commit_sha.in_(wanted),
                )
            )
        )
        for (issue_id,) in rows.all():
            found.setdefault(issue_id, source)
    if release.sha:
        reached = await _default_branch_time(session, integration, release.sha)
        if reached is not None:
            for issue_id in await _released_by_default_branch(session, integration, release, reached):
                found.setdefault(issue_id, "default_branch")
    if not found:
        return 0
    versions = await _matching_versions(session, release, set(found))
    inserted = await session.execute(
        pg_insert(IssueReleaseLink)
        .values(
            [
                {"release_id": release.id, "issue_id": issue_id, "source": source, "version_id": versions.get(issue_id)}
                for issue_id, source in found.items()
            ]
        )
        .on_conflict_do_nothing()
        .returning(IssueReleaseLink.issue_id)
    )
    new_issue_ids = [issue_id for (issue_id,) in inserted.all()]
    if new_issue_ids:
        await _prune_issue_releases(session, new_issue_ids)
    return len(new_issue_ids)


async def _released_by_default_branch(
    session: AsyncSession, integration: RepositoryIntegration, release: RepositoryRelease, reached: datetime
) -> list[int]:
    """Issues whose work reached the default branch by *reached* and are in no other tag of the repository yet."""
    commits = (
        select(IssueCommitLink.issue_id.label("issue_id"), RepositoryCommit.created_at.label("at"))
        .join(RepositoryCommit, RepositoryCommit.id == IssueCommitLink.commit_id)
        .where(
            RepositoryCommit.integration_id == integration.id,
            RepositoryCommit.branch == integration.default_branch,
            RepositoryCommit.created_at <= reached,
        )
    )
    merged_at = func.coalesce(PullRequest.merged_at, PullRequest.provider_updated_at, PullRequest.updated_at)
    prs = (
        select(IssuePullRequestLink.issue_id.label("issue_id"), merged_at.label("at"))
        .join(PullRequest, PullRequest.id == IssuePullRequestLink.pull_request_id)
        .where(
            PullRequest.integration_id == integration.id,
            PullRequest.provider == integration.provider,
            PullRequest.state == PR_MERGED,
            PullRequest.target_branch == integration.default_branch,
            merged_at <= reached,
        )
    )
    both = union_all(commits, prs).subquery()
    already = (
        select(IssueReleaseLink.issue_id)
        .join(RepositoryRelease, RepositoryRelease.id == IssueReleaseLink.release_id)
        .where(RepositoryRelease.integration_id == integration.id, RepositoryRelease.id != release.id)
    )
    rows = await session.execute(
        select(both.c.issue_id)
        .where(both.c.issue_id.not_in(already))
        .group_by(both.c.issue_id)
        .order_by(func.max(both.c.at).desc())
        .limit(c.DEVELOPMENT_RELEASE_MAX_ISSUES_PER_TAG)
    )
    return [issue_id for (issue_id,) in rows.all()]


def _version_names(release: RepositoryRelease) -> set[str]:
    names = {release.tag_name}
    for name in (release.tag_name, release.name):
        if name:
            names.add(name)
            if name[:1] in ("v", "V") and name[1:2].isdigit():
                names.add(name[1:])
    return names


async def _matching_versions(session: AsyncSession, release: RepositoryRelease, issue_ids: set[int]) -> dict[int, int]:
    """Issue id → the id of its project's version named like the tag or release; link, never create."""
    rows = await session.execute(
        select(Issue.id, Version.id)
        .join(Version, Version.project_id == Issue.project_id)
        .where(Issue.id.in_(issue_ids), Version.name.in_(_version_names(release)))
        .order_by(Version.id)
    )
    found: dict[int, int] = {}
    for issue_id, version_id in rows.all():
        found.setdefault(issue_id, version_id)
    return found


async def _prune_issue_releases(session: AsyncSession, issue_ids: Sequence[int]) -> None:
    """Keep each issue's newest ``DEVELOPMENT_RELEASES_PER_ISSUE`` release links."""
    ranked = (
        select(
            IssueReleaseLink.release_id,
            IssueReleaseLink.issue_id,
            func.row_number()
            .over(
                partition_by=IssueReleaseLink.issue_id,
                order_by=(IssueReleaseLink.created_at.desc(), IssueReleaseLink.release_id.desc()),
            )
            .label("rank"),
        )
        .where(IssueReleaseLink.issue_id.in_(issue_ids))
        .subquery()
    )
    overflow = select(ranked.c.release_id, ranked.c.issue_id).where(ranked.c.rank > c.DEVELOPMENT_RELEASES_PER_ISSUE)
    await session.execute(
        delete(IssueReleaseLink).where(tuple_(IssueReleaseLink.release_id, IssueReleaseLink.issue_id).in_(overflow))
    )


# ---------------------------------------------------------------------------
# Detachments
# ---------------------------------------------------------------------------


async def detached_pairs(
    session: AsyncSession, item_type: str, pairs: Iterable[tuple[int, int]]
) -> set[tuple[int, int]]:
    """The (item id, issue id) pairs of *pairs* that someone detached; they are never linked again."""
    wanted = set(pairs)
    if not wanted:
        return set()
    rows = await session.execute(
        select(DevelopmentDetachment.item_id, DevelopmentDetachment.issue_id).where(
            DevelopmentDetachment.item_type == item_type,
            tuple_(DevelopmentDetachment.item_id, DevelopmentDetachment.issue_id).in_(wanted),
        )
    )
    return {(item_id, issue_id) for item_id, issue_id in rows.all()}


ITEM_COMMIT = "commit"
ITEM_PULL_REQUEST = "pull_request"


@dataclass
class _Item:
    item_type: str
    item_id: int
    integration: RepositoryIntegration
    label: str  # short SHA or #number, for audit details
    metadata_key: str
    metadata_value: str


async def _linked_item(session: AsyncSession, item_type: str, item_id: int, issue: Issue) -> _Item:
    """The commit or pull request *item_id* linked to *issue*; NotFoundError otherwise."""
    from specivo.services.repository_hooks import COMMITS_METADATA_KEY
    from specivo.services.repository_pull_requests import PULL_REQUESTS_METADATA_KEY

    if item_type == ITEM_COMMIT:
        commit_row = (
            await session.execute(
                select(RepositoryCommit, RepositoryIntegration)
                .join(IssueCommitLink, IssueCommitLink.commit_id == RepositoryCommit.id)
                .join(RepositoryIntegration, RepositoryIntegration.id == RepositoryCommit.integration_id)
                .where(IssueCommitLink.commit_id == item_id, IssueCommitLink.issue_id == issue.id)
            )
        ).first()
        if commit_row is None:
            raise NotFoundError("Commit not linked to this issue")
        commit, integration = commit_row
        return _Item(item_type, item_id, integration, commit.sha, COMMITS_METADATA_KEY, commit.sha)
    pr_row = (
        await session.execute(
            select(PullRequest, RepositoryIntegration)
            .join(IssuePullRequestLink, IssuePullRequestLink.pull_request_id == PullRequest.id)
            .join(RepositoryIntegration, RepositoryIntegration.id == PullRequest.integration_id)
            .where(IssuePullRequestLink.pull_request_id == item_id, IssuePullRequestLink.issue_id == issue.id)
        )
    ).first()
    if pr_row is None:
        raise NotFoundError("Pull request not linked to this issue")
    pr, integration = pr_row
    sigil = "!" if pr.provider == "gitlab" else "#"
    return _Item(
        item_type,
        item_id,
        integration,
        f"{sigil}{pr.number}",
        PULL_REQUESTS_METADATA_KEY,
        pr.url or f"{sigil}{pr.number}",
    )


def _remove_metadata(issue: Issue, key: str, value: str) -> None:
    metadata = dict(issue.issue_metadata or {})
    current = metadata.get(key)
    if isinstance(current, list) and value in current:
        metadata[key] = [v for v in current if v != value]
        issue.issue_metadata = metadata


async def detach_item(
    session: AsyncSession,
    issue: Issue,
    item_type: str,
    item_id: int,
    *,
    actor: User,
    target: Issue | None = None,
    request: Request | None = None,
) -> None:
    """Detach a commit or pull request from *issue*, and link it to *target* when given (a move).

    The caller has checked that *actor* may edit both issues. The detachment
    is kept, so a later delivery of the same push or pull request never links
    the pair again. A move is refused (422) when *target* is outside the
    repository's scope. The item's SHA or URL leaves the issue's ``commits`` or
    ``pull_requests`` metadata and is added to the target's. A security audit
    row records it.
    """
    from specivo.services.repository_hooks import append_metadata, scope_project_keys

    item = await _linked_item(session, item_type, item_id, issue)
    if target is not None:
        if target.id == issue.id:
            raise ValidationError("The target is the same issue", field="target_issue_ref")
        scope = await scope_project_keys(session, item.integration)
        if target.project_id not in set(scope.values()):
            raise ValidationError(
                "The target issue is not in a project this repository may link to", field="target_issue_ref"
            )
    link_model = IssueCommitLink if item_type == ITEM_COMMIT else IssuePullRequestLink
    item_column = IssueCommitLink.commit_id if item_type == ITEM_COMMIT else IssuePullRequestLink.pull_request_id
    await session.execute(delete(link_model).where(item_column == item_id, link_model.issue_id == issue.id))
    await session.execute(
        pg_insert(DevelopmentDetachment)
        .values(item_type=item_type, item_id=item_id, issue_id=issue.id, detached_by_id=actor.id)
        .on_conflict_do_nothing(constraint="uq_development_detachments_item_issue")
    )
    _remove_metadata(issue, item.metadata_key, item.metadata_value)
    if target is not None:
        # A person's explicit choice wins over an earlier detachment from the target.
        await session.execute(
            delete(DevelopmentDetachment).where(
                DevelopmentDetachment.item_type == item_type,
                DevelopmentDetachment.item_id == item_id,
                DevelopmentDetachment.issue_id == target.id,
            )
        )
        values: dict[str, Any] = {"issue_id": target.id, "source": "manual"}
        if item_type == ITEM_COMMIT:
            stmt = pg_insert(IssueCommitLink).values(commit_id=item_id, keyword=None, **values)
        else:
            stmt = pg_insert(IssuePullRequestLink).values(pull_request_id=item_id, closing=False, **values)
        await session.execute(stmt.on_conflict_do_nothing())
        append_metadata(target, {item.metadata_key: [item.metadata_value]})
    await session.flush()
    await _audit(session, issue, item, actor, target, request)
    logger.info(
        "Development %s %d %s from issue %d%s",
        item_type,
        item_id,
        "moved" if target else "detached",
        issue.id,
        f" to {target.id}" if target else "",
    )


async def _audit(
    session: AsyncSession, issue: Issue, item: _Item, actor: User, target: Issue | None, request: Request | None
) -> None:
    """The audit row of a detach or move; a failed audit never fails the change."""
    from specivo.services.security_audit_service import SecurityAuditService

    try:
        await SecurityAuditService().log_development_link_change(
            session,
            user_id=actor.id,
            project_id=issue.project_id,
            issue_id=issue.id,
            issue_ref=issue.display_key,
            item_type=item.item_type,
            item_id=item.item_id,
            item_ref=item.label,
            repository=item.integration.repo_full_name,
            target_issue_ref=target.display_key if target else None,
            target_project_id=target.project_id if target else None,
            request=request,
        )
    except Exception:
        logger.warning("Audit of a development link change on issue %d failed", issue.id, exc_info=True)


# ---------------------------------------------------------------------------
# Reading: what belongs to an issue
# ---------------------------------------------------------------------------


def _linked_shas(issue_id: int) -> Select:
    """(integration id, sha) of the issue's commits and its pull requests' merge commits."""
    return union_all(
        select(RepositoryCommit.integration_id, RepositoryCommit.sha)
        .join(IssueCommitLink, IssueCommitLink.commit_id == RepositoryCommit.id)
        .where(IssueCommitLink.issue_id == issue_id),
        select(PullRequest.integration_id, PullRequest.merge_commit_sha)
        .join(IssuePullRequestLink, IssuePullRequestLink.pull_request_id == PullRequest.id)
        .where(IssuePullRequestLink.issue_id == issue_id, PullRequest.merge_commit_sha.is_not(None)),
    )  # type: ignore[return-value]


def _linked_branches(issue_id: int) -> Select:
    """(integration id, branch): linked requests' source branches and branches commits were linked by, not default."""
    return union_all(
        select(PullRequest.integration_id, PullRequest.source_branch)
        .join(IssuePullRequestLink, IssuePullRequestLink.pull_request_id == PullRequest.id)
        .join(RepositoryIntegration, RepositoryIntegration.id == PullRequest.integration_id)
        .where(
            IssuePullRequestLink.issue_id == issue_id,
            PullRequest.source_branch.is_not(None),
            PullRequest.source_branch != RepositoryIntegration.default_branch,
        ),
        select(RepositoryCommit.integration_id, RepositoryCommit.branch)
        .join(IssueCommitLink, IssueCommitLink.commit_id == RepositoryCommit.id)
        .join(RepositoryIntegration, RepositoryIntegration.id == RepositoryCommit.integration_id)
        .where(
            IssueCommitLink.issue_id == issue_id,
            IssueCommitLink.source == "branch",
            RepositoryCommit.branch.is_not(None),
            RepositoryCommit.branch != RepositoryIntegration.default_branch,
        ),
    )  # type: ignore[return-value]


def _linked_pr_numbers(issue_id: int) -> Select:
    return (
        select(PullRequest.integration_id, PullRequest.provider, PullRequest.number)
        .join(IssuePullRequestLink, IssuePullRequestLink.pull_request_id == PullRequest.id)
        .where(IssuePullRequestLink.issue_id == issue_id)
    )


def ci_runs_clause(issue_id: int):
    return or_(
        tuple_(CiRun.integration_id, CiRun.head_sha).in_(_linked_shas(issue_id)),
        tuple_(CiRun.integration_id, CiRun.provider, CiRun.pull_request_number).in_(_linked_pr_numbers(issue_id)),
        tuple_(CiRun.integration_id, CiRun.branch).in_(_linked_branches(issue_id)),
    )


def deployments_clause(issue_id: int):
    return or_(
        tuple_(Deployment.integration_id, Deployment.sha).in_(_linked_shas(issue_id)),
        tuple_(Deployment.integration_id, Deployment.ref).in_(_linked_branches(issue_id)),
    )


def reviews_query(issue_id: int) -> Select:
    return (
        select(PullRequestReview, PullRequest)
        .join(PullRequest, PullRequest.id == PullRequestReview.pull_request_id)
        .join(IssuePullRequestLink, IssuePullRequestLink.pull_request_id == PullRequest.id)
        .where(IssuePullRequestLink.issue_id == issue_id)
    )


def releases_query(issue_id: int) -> Select:
    return (
        select(RepositoryRelease, IssueReleaseLink.source, IssueReleaseLink.version_id, Version.name)
        .join(IssueReleaseLink, IssueReleaseLink.release_id == RepositoryRelease.id)
        .outerjoin(Version, Version.id == IssueReleaseLink.version_id)
        .where(IssueReleaseLink.issue_id == issue_id)
    )


# Sort times of each kind, used both in SQL ordering and in the merged timeline.
_COMMIT_AT = func.coalesce(RepositoryCommit.committed_at, RepositoryCommit.created_at)
_PR_AT = func.coalesce(PullRequest.merged_at, PullRequest.provider_updated_at, PullRequest.updated_at)
_RELEASE_AT = func.coalesce(RepositoryRelease.released_at, RepositoryRelease.occurred_at)


@dataclass(frozen=True)
class TimelineRow:
    """One timeline entry before it becomes ``DevelopmentTimelineItemOut``."""

    kind: str
    id: int
    occurred_at: datetime
    title: str
    state: str | None
    url: str
    integration_id: int
    provider: str
    sha: str | None = None
    branch: str | None = None
    number: int | None = None
    actor: str | None = None


async def _count(session: AsyncSession, query: Select) -> int:
    return int(await session.scalar(select(func.count()).select_from(query.subquery())) or 0)


async def _timeline_commits(session: AsyncSession, issue_id: int, top: int) -> tuple[int, list[TimelineRow]]:
    query = (
        select(RepositoryCommit, _COMMIT_AT.label("at"))
        .join(IssueCommitLink, IssueCommitLink.commit_id == RepositoryCommit.id)
        .where(IssueCommitLink.issue_id == issue_id)
    )
    rows = await session.execute(query.order_by(_COMMIT_AT.desc(), RepositoryCommit.id.desc()).limit(top))
    return await _count(session, query), [
        TimelineRow(
            "commit",
            commit.id,
            at,
            commit.first_line,
            None,
            commit.url,
            commit.integration_id,
            "",
            sha=commit.sha,
            branch=commit.branch,
            actor=commit.author_name or None,
        )
        for commit, at in rows.all()
    ]


async def _timeline_pull_requests(session: AsyncSession, issue_id: int, top: int) -> tuple[int, list[TimelineRow]]:
    query = (
        select(PullRequest, _PR_AT.label("at"))
        .join(IssuePullRequestLink, IssuePullRequestLink.pull_request_id == PullRequest.id)
        .where(IssuePullRequestLink.issue_id == issue_id)
    )
    rows = await session.execute(query.order_by(_PR_AT.desc(), PullRequest.id.desc()).limit(top))
    return await _count(session, query), [
        TimelineRow(
            "pull_request",
            pr.id,
            at,
            pr.title,
            pr.state,
            pr.url,
            pr.integration_id,
            pr.provider,
            branch=pr.source_branch,
            number=pr.number,
            actor=pr.author_login,
        )
        for pr, at in rows.all()
    ]


async def _timeline_reviews(session: AsyncSession, issue_id: int, top: int) -> tuple[int, list[TimelineRow]]:
    query = reviews_query(issue_id)
    rows = await session.execute(
        query.order_by(PullRequestReview.submitted_at.desc(), PullRequestReview.id.desc()).limit(top)
    )
    return await _count(session, query), [
        TimelineRow(
            "review",
            review.id,
            review.submitted_at,
            pr.title,
            review.state,
            review.url or pr.url,
            pr.integration_id,
            pr.provider,
            number=pr.number,
            actor=review.reviewer_login,
        )
        for review, pr in rows.all()
    ]


async def _timeline_ci(session: AsyncSession, issue_id: int, top: int) -> tuple[int, list[TimelineRow]]:
    query = select(CiRun).where(ci_runs_clause(issue_id))
    rows = await session.execute(query.order_by(CiRun.occurred_at.desc(), CiRun.id.desc()).limit(top))
    return await _count(session, query), [
        TimelineRow(
            "ci",
            run.id,
            run.occurred_at,
            run.name,
            run.status,
            run.url,
            run.integration_id,
            run.provider,
            sha=run.head_sha,
            branch=run.branch,
            number=run.pull_request_number,
        )
        for run in rows.scalars()
    ]


async def _timeline_deployments(session: AsyncSession, issue_id: int, top: int) -> tuple[int, list[TimelineRow]]:
    query = select(Deployment).where(deployments_clause(issue_id))
    rows = await session.execute(query.order_by(Deployment.occurred_at.desc(), Deployment.id.desc()).limit(top))
    return await _count(session, query), [
        TimelineRow(
            "deployment",
            deployment.id,
            deployment.occurred_at,
            deployment.environment,
            deployment.status,
            deployment.url,
            deployment.integration_id,
            deployment.provider,
            sha=deployment.sha,
            branch=deployment.ref,
        )
        for deployment in rows.scalars()
    ]


async def _timeline_releases(session: AsyncSession, issue_id: int, top: int) -> tuple[int, list[TimelineRow]]:
    query = releases_query(issue_id)
    rows = await session.execute(
        query.add_columns(_RELEASE_AT.label("at")).order_by(_RELEASE_AT.desc(), RepositoryRelease.id.desc()).limit(top)
    )
    return await _count(session, query), [
        TimelineRow(
            "release",
            release.id,
            at,
            release.tag_name,
            "released" if release.is_release else "tagged",
            release.url,
            release.integration_id,
            release.provider,
            sha=release.sha,
        )
        for release, _source, _version_id, _version_name, at in rows.all()
    ]


_TIMELINE_READERS = {
    "commit": _timeline_commits,
    "pull_request": _timeline_pull_requests,
    "review": _timeline_reviews,
    "ci": _timeline_ci,
    "deployment": _timeline_deployments,
    "release": _timeline_releases,
}


_KIND_ORDER = {kind: i for i, kind in enumerate(("release", "deployment", "ci", "review", "pull_request", "commit"))}


async def timeline(
    session: AsyncSession, issue_id: int, kinds: Iterable[str], offset: int, limit: int
) -> tuple[int, list[TimelineRow]]:
    """All linked activity of *kinds*, newest first: (total, the page at *offset*).

    Each kind is read newest first up to ``offset + limit`` rows, so the
    merged page is exact without reading everything.
    """
    top = offset + limit
    total = 0
    lists: list[list[TimelineRow]] = []
    for kind in kinds:
        count, rows = await _TIMELINE_READERS[kind](session, issue_id, top)
        total += count
        lists.append(rows)

    def key(row: TimelineRow) -> tuple:
        return (-row.occurred_at.timestamp(), _KIND_ORDER[row.kind], -row.id)

    merged = list(heapq.merge(*lists, key=key))
    return total, merged[offset:top]
