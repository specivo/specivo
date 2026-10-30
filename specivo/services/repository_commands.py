"""What a repository may change on an issue: commit commands and merge automation.

Both are off by default per integration and never bypass a check. Each
returns a *reason* when it did nothing, recorded in the delivery log and the
structured log; the forge is told nothing.

Commit commands (``commands_enabled``) run once per (commit, issue) link, as
the commit's *matched user*, the Specivo account whose verified email is the
commit author email:

- ``Fixes KEY-1`` on the default branch moves the issue to the integration's
  ``fixing_status_id``, through the regular update path: the user needs
  ``edit_issues`` and a workflow transition, like in the UI.
- ``KEY-1 @1h30m`` (any branch, with ``time_logging_enabled`` as well) logs
  the time for that user, who needs ``log_time``. The entry is dated on the
  commit day in the user's timezone, with the default activity, and the
  commit's first line and short SHA as its comment.

The commit author email is not authenticated, so turning commands on means
anyone with push access may act as any project member with a verified email;
the settings UI says so.

Merge automation (``merge_automation_enabled``): when a pull request whose
link is *closing* merges into the default branch, the issue moves to
``merge_status_id``. It runs as ``$git`` with the project role a manager gave
it, never as the pull request's author, through the same update path and
workflow validation. ``$git`` is never an administrator, so nothing is
bypassed. A refused transition leaves the status alone and writes one note
on the issue saying why.
"""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import AppError, NotFoundError, PermissionDeniedError
from specivo.core.utils import utcnow
from specivo.hooks.refs import Ref
from specivo.models.issue import Issue
from specivo.models.lookups import IssueStatus
from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.repository import PullRequest, RepositoryCommit, RepositoryIntegration
from specivo.models.role import Role
from specivo.models.user import User
from specivo.schemas.repository import RepositorySettings

logger = logging.getLogger(__name__)

#: The issue was moved; anything else returned by these functions is a reason for doing nothing.
MOVED = "moved"
LOGGED = "logged"
_DONE = (MOVED, LOGGED)

_TIME_COMMENT_MAX_CHARS = 200


def integration_settings(integration: RepositoryIntegration) -> RepositorySettings:
    return RepositorySettings.model_validate(integration.settings or {}, strict=False)


async def member_roles(session: AsyncSession, user: User, project_id: int) -> list[Role]:
    """The roles *user*'s own membership holds in the project (no group, no Non member fallback)."""
    rows = await session.execute(
        select(Role)
        .join(MemberRole, MemberRole.role_id == Role.id)
        .join(Member, Member.id == MemberRole.member_id)
        .where(Member.user_id == user.id, Member.project_id == project_id)
        .order_by(Role.position, Role.id)
    )
    return list(rows.scalars().unique().all())


async def _status_checks(session: AsyncSession, issue: Issue, target_status_id: int) -> str | None:
    """``already_closed``, ``already_in_status`` or ``status_missing``; None when the move may go ahead."""
    if issue.status_id == target_status_id:
        return "already_in_status"
    current = await session.get(IssueStatus, issue.status_id)
    if current is not None and current.is_closed:
        return "already_closed"
    if await session.get(IssueStatus, target_status_id) is None:
        return "status_missing"
    return None


async def _move(session: AsyncSession, issue: Issue, status_id: int, user: User, notes: str) -> str:
    """Change the status as *user* through ``IssueService``; a refusal leaves everything as it was.

    ``update_with_journal`` checks the workflow transition and field rules
    before it changes or writes anything, so a refusal needs no rollback.
    """
    from specivo.schemas.issue import IssueUpdate
    from specivo.services.issue_service import IssueService

    try:
        await IssueService().update_with_journal(
            session,
            issue,
            IssueUpdate.model_validate({"status_id": status_id, "lock_version": issue.lock_version}),
            user,
            notes=notes,
        )
    except AppError as exc:
        if exc.code == "workflow_transition_denied":
            return "transition_not_allowed"
        return f"refused:{exc.code}"
    return MOVED


# ---------------------------------------------------------------------------
# Merge automation
# ---------------------------------------------------------------------------


def merge_applies(integration: RepositoryIntegration, pr: PullRequest) -> bool:
    """Whether merge automation is on and *pr* is a merge into the default branch."""
    settings = integration_settings(integration)
    return (
        settings.merge_automation_enabled
        and settings.merge_status_id is not None
        and pr.state == "merged"
        and pr.target_branch == integration.default_branch
    )


async def run_merge_automation(
    session: AsyncSession,
    integration: RepositoryIntegration,
    pr: PullRequest,
    issue: Issue,
    git_user: User,
    *,
    pr_label: str,
    note: bool = True,
) -> str:
    """Move *issue* to the merge status as ``$git``; returns ``moved`` or why not.

    The caller has checked :func:`merge_applies` and that the link qualified
    at the merge. An issue ``$git`` may not see (a private one) is never
    touched: no status check, note or move.
    A move the update path refused (:func:`is_refusal`) writes a note on the
    issue saying why, unless *note* is false (the issue already has one).
    """
    from specivo.services.issue_service import IssueService
    from specivo.services.journal_service import JournalService
    from specivo.services.permission_service import Permission, check_permission

    status_id = integration_settings(integration).merge_status_id
    assert status_id is not None  # merge_applies
    try:
        await IssueService().get_by_id(session, issue.id, user=git_user)
    except NotFoundError:
        return "issue_not_visible"
    reason = await _status_checks(session, issue, status_id)
    if reason is not None:
        return reason
    roles = await member_roles(session, git_user, issue.project_id)
    if not roles:
        return "no_git_membership"
    if not await check_permission(git_user, issue.project_id, Permission.EDIT_ISSUES, session):
        return "missing_permission:edit_issues"

    merged = f"{pr_label} merged into `{_code(pr.target_branch or '')}`"
    if pr.merged_by_login:
        merged += f" by `{_code(pr.merged_by_login)}`"
    outcome = await _move(session, issue, status_id, git_user, notes=merged + ".")
    if outcome == MOVED or not note:
        return outcome
    target = await session.get(IssueStatus, status_id)
    why = (
        "transition not allowed for the " + ", ".join(role.name for role in roles) + " role"
        if outcome == "transition_not_allowed"
        else outcome
    )
    await JournalService().add_comment(
        session, issue, git_user, f"{merged}; could not move to {target.name if target else status_id}: {why}."
    )
    return outcome


def _code(value: str) -> str:
    return value.replace("`", "")


# ---------------------------------------------------------------------------
# Commit commands
# ---------------------------------------------------------------------------


async def _matched_user(session: AsyncSession, commit: RepositoryCommit) -> User | None:
    if commit.matched_user_id is None:
        return None
    user = await session.get(User, commit.matched_user_id)
    return user if user is not None and user.status == "active" else None


async def run_commit_commands(
    session: AsyncSession,
    integration: RepositoryIntegration,
    commit: RepositoryCommit,
    issue: Issue,
    ref: Ref,
    *,
    commit_label: str,
) -> list[tuple[str, str]]:
    """Run the commands *ref* carries for a newly linked commit; returns (command, outcome) pairs.

    Only a reference in the message carries commands (``ref`` from a branch
    name is always a plain ``refs``).
    """
    wants_fix = ref.keyword == "fixes"
    wants_time = ref.hours is not None
    if not (wants_fix or wants_time):
        return []
    settings = integration_settings(integration)
    if not settings.commands_enabled:
        return [("commands", "commands_disabled")]
    outcomes: list[tuple[str, str]] = []
    user = await _matched_user(session, commit)
    if wants_fix:
        outcomes.append(("fixes", await _fix(session, integration, settings, commit, issue, user, commit_label)))
    if wants_time:
        assert ref.hours is not None
        if not settings.time_logging_enabled:
            outcomes.append(("time", "time_logging_disabled"))
        else:
            outcomes.append(("time", await _log_time(session, commit, issue, user, ref)))
    return outcomes


async def _fix(
    session: AsyncSession,
    integration: RepositoryIntegration,
    settings: RepositorySettings,
    commit: RepositoryCommit,
    issue: Issue,
    user: User | None,
    commit_label: str,
) -> str:
    from specivo.services.issue_service import IssueService
    from specivo.services.permission_service import Permission, check_permission

    if commit.branch != integration.default_branch:
        return "not_default_branch"
    if settings.fixing_status_id is None:
        return "no_fixing_status"
    if user is None:
        return "no_matched_user"
    try:
        await IssueService().get_by_id(session, issue.id, user=user)
    except NotFoundError:
        return "issue_not_visible"
    if not await check_permission(user, issue.project_id, Permission.EDIT_ISSUES, session):
        return "missing_permission:edit_issues"
    reason = await _status_checks(session, issue, settings.fixing_status_id)
    if reason is not None:
        return reason
    return await _move(session, issue, settings.fixing_status_id, user, notes=f"Fixed in {commit_label}.")


async def _default_activity_id(session: AsyncSession) -> int | None:
    from specivo.services.time_entry_service import TimeEntryService

    activity = await TimeEntryService().default_activity(session)
    return None if activity is None else activity.id


def _local_date(moment: datetime | None, timezone: str) -> datetime:
    try:
        tz = ZoneInfo(timezone or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo("UTC")
    return (moment or utcnow()).astimezone(tz)


async def _log_time(session: AsyncSession, commit: RepositoryCommit, issue: Issue, user: User | None, ref: Ref) -> str:
    from specivo.schemas.time_entry import TimeEntryCreate
    from specivo.services.time_entry_service import TimeEntryService

    if user is None:
        return "no_matched_user"
    project = await session.get(Project, issue.project_id)
    if project is None:  # pragma: no cover - the issue is deleted with its project
        return "issue_not_visible"
    service = TimeEntryService()
    try:
        await service.require_can_log(session, user, project, issue.id)
    except NotFoundError:
        return "issue_not_visible"
    except PermissionDeniedError:
        return "missing_permission:log_time"
    except AppError as exc:
        return f"refused:{exc.code}"
    activity_id = await _default_activity_id(session)
    if activity_id is None:
        return "no_activity"
    first_line = commit.first_line
    if len(first_line) > _TIME_COMMENT_MAX_CHARS:
        first_line = first_line[: _TIME_COMMENT_MAX_CHARS - 1] + "…"
    comment = f"{first_line} ({commit.short_sha})" if first_line else commit.short_sha
    assert ref.hours is not None
    await service.create(
        session,
        project.id,
        TimeEntryCreate(
            issue_id=issue.id,
            activity_id=activity_id,
            hours=ref.hours,
            comments=comment,
            spent_on=_local_date(commit.committed_at, user.timezone).date(),
        ),
        user,
    )
    return LOGGED


def describe(outcomes: list[tuple[str, str, str]]) -> str:
    """``KEY-1 fixes: no_matched_user; KEY-2 time: logged`` for the delivery log."""
    return "; ".join(f"{key} {command}: {outcome}" for key, command, outcome in outcomes)


def is_done(outcome: str) -> bool:
    return outcome in _DONE


def is_refusal(outcome: str) -> bool:
    """Whether the regular update path refused the move (workflow or another check)."""
    return outcome == "transition_not_allowed" or outcome.startswith("refused:")
