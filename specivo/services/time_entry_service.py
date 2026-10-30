"""Time entry service — CRUD for time entries and timer operations.

Which time entries a user may read is decided here, in
``visible_entries_clause`` (one project) and ``visible_entries_clause_any``
(across projects), from the roles ``permission_service.get_user_roles``
resolves that grant ``view_time_entries``. Every reader goes through one of
them: the project list (web and REST), a single entry, the issue's time tab
and its logged-time sum, a version's logged-time sum, the billing report and
the dashboard's agent feed.

Who may write is decided here too: ``require_can_log`` (``log_time``) for
every new entry written on a user's behalf, timers included, and
``can_edit`` (``manage_time_entries``, or one's own entry with ``log_time``)
for edits and deletes. ``create`` itself checks nothing, because the
importer writes entries for their original authors.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

from fastapi import Request
from sqlalchemy import and_, false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.sql.elements import ColumnElement

from specivo.core.config import get_settings
from specivo.core.exceptions import AppError, NotFoundError, PermissionDeniedError, ValidationError
from specivo.core.utils import utcnow
from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.role import TIME_ENTRIES_VISIBILITY_ALL, TIME_ENTRIES_VISIBILITY_OWN, Role
from specivo.models.time_entry import ActiveTimer, TimeEntry, TimeEntryActivity
from specivo.models.user import User
from specivo.schemas.time_entry import TimeEntryCreate, TimeEntryUpdate
from specivo.services.api_key_scope import admin_reaches, project_in_scope, scope_clause

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Timer calculation helper (module-level for unit testing)
# ---------------------------------------------------------------------------

_MAX_TIMER_HOURS: Decimal | None = None


def _get_max_timer_hours() -> Decimal:
    global _MAX_TIMER_HOURS
    if _MAX_TIMER_HOURS is None:
        _MAX_TIMER_HOURS = Decimal(str(get_settings().timer_max_hours))
    return _MAX_TIMER_HOURS


def _compute_elapsed_hours(started_at: datetime, now: datetime) -> Decimal:
    """Compute elapsed hours between two datetimes.

    Caps at 12 hours for overnight protection.
    Returns Decimal with 2 decimal places.
    """
    elapsed_seconds = (now - started_at).total_seconds()
    raw_hours = Decimal(str(elapsed_seconds)) / Decimal("3600")
    hours = raw_hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    if hours > _get_max_timer_hours():
        return _get_max_timer_hours()

    return hours


# ---------------------------------------------------------------------------
# Service class
# ---------------------------------------------------------------------------


class TimeEntryService:
    """Service layer for time entry and timer operations."""

    # -------------------------------------------------------------------
    # Visibility
    # -------------------------------------------------------------------

    async def visible_entries_clause(
        self, session: AsyncSession, user: User, project: Project
    ) -> ColumnElement[bool] | None:
        """Return the predicate limiting *project*'s time entries to those *user* may read, or None.

        None means every entry (admins, or a role granting
        ``view_time_entries`` with ``time_entries_visibility = 'all'``); the
        user's own entries when every such role is ``'own'``; and nothing at
        all without ``view_time_entries``. Reaching the project is the
        caller's job (``ProjectService.require_project_access``).
        """
        from specivo.services.permission_service import time_entries_visibility

        visibility = await time_entries_visibility(session, user, project)
        if visibility is None:
            return false()
        if visibility == TIME_ENTRIES_VISIBILITY_OWN:
            return TimeEntry.user_id == user.id
        return None

    async def visible_entries_clause_any(self, session: AsyncSession, user: User) -> ColumnElement[bool] | None:
        """Return the cross-project form of ``visible_entries_clause``, or None for no limit (admins).

        Mirrors ``get_user_roles`` + ``best_time_entries_visibility`` in SQL,
        the way ``IssueService.visible_issues_clause`` does for issues: only
        roles granting ``view_time_entries`` count. A project the user is a
        member of shows every entry or only the user's own, following those
        membership roles; a public project without a membership follows the
        Non member role the same way. Any other project shows nothing. It is
        an allow-list by project, but callers should still limit the query to
        projects the user can reach. Everyone is narrowed to the
        authenticating API key's project limit (``specivo.services.api_key_scope``).
        """
        clause = await self._visible_entries_clause_any(session, user)
        limit = scope_clause(TimeEntry.project_id)
        if limit is None:
            return clause
        return limit if clause is None else and_(clause, limit)

    async def _visible_entries_clause_any(self, session: AsyncSession, user: User) -> ColumnElement[bool] | None:
        """``visible_entries_clause_any`` before the API key project limit is applied."""
        if user.is_admin:
            return None
        if user.is_anonymous:
            return false()

        from specivo.services.permission_service import (
            Permission,
            best_time_entries_visibility,
            get_non_member_role,
            member_principal_clause,
            role_grants,
        )

        rows = await session.execute(
            select(Member.project_id, Role.permissions, Role.time_entries_visibility)
            .select_from(Member)
            .outerjoin(MemberRole, MemberRole.member_id == Member.id)
            .outerjoin(Role, Role.id == MemberRole.role_id)
            .where(member_principal_clause(user.id))
        )
        by_project: dict[int, set[str]] = {}
        for project_id, permissions, visibility in rows:
            values = by_project.setdefault(project_id, set())
            if visibility is not None and role_grants(permissions or (), Permission.VIEW_TIME_ENTRIES):
                values.add(visibility)

        best = {project_id: best_time_entries_visibility(values) for project_id, values in by_project.items()}
        everything = sorted(pid for pid, value in best.items() if value == TIME_ENTRIES_VISIBILITY_ALL)
        own_only = sorted(pid for pid, value in best.items() if value == TIME_ENTRIES_VISIBILITY_OWN)

        visible: list[ColumnElement[bool]] = []
        if everything:
            visible.append(TimeEntry.project_id.in_(everything))
        if own_only:
            visible.append(and_(TimeEntry.project_id.in_(own_only), TimeEntry.user_id == user.id))

        non_member = await get_non_member_role(session)
        if non_member is not None and non_member.grants(Permission.VIEW_TIME_ENTRIES):
            public_without_membership = select(Project.id).where(Project.is_public.is_(True))
            if by_project:
                public_without_membership = public_without_membership.where(Project.id.not_in(sorted(by_project)))
            public_clause: ColumnElement[bool] = TimeEntry.project_id.in_(public_without_membership)
            if non_member.time_entries_visibility == TIME_ENTRIES_VISIBILITY_OWN:
                public_clause = and_(public_clause, TimeEntry.user_id == user.id)
            visible.append(public_clause)

        if not visible:
            return false()
        return or_(*visible)

    async def get_visible(self, session: AsyncSession, entry_id: int, user: User) -> TimeEntry:
        """Return time entry *entry_id* if *user* may read it, else raise ``NotFoundError``.

        404 rather than 403 for an entry in an unreachable project, in a
        project where the user lacks ``view_time_entries``, or outside the
        user's visibility, so entry ids cannot be probed.
        """
        from specivo.services.permission_service import time_entries_visibility
        from specivo.services.project_service import ProjectService

        entry = await self.get_by_id(session, entry_id)
        project = await session.get(Project, entry.project_id)
        if project is None:
            raise NotFoundError(f"Time entry {entry_id} not found")
        try:
            await ProjectService().require_project_access(session, project, user)
        except NotFoundError:
            raise NotFoundError(f"Time entry {entry_id} not found") from None
        visibility = await time_entries_visibility(session, user, project)
        if visibility is None or (visibility == TIME_ENTRIES_VISIBILITY_OWN and entry.user_id != user.id):
            raise NotFoundError(f"Time entry {entry_id} not found")
        return entry

    # -------------------------------------------------------------------
    # Permissions to write
    # -------------------------------------------------------------------

    async def require_can_log(
        self,
        session: AsyncSession,
        user: User,
        project: Project,
        issue_id: int | None = None,
        *,
        request: Request | None = None,
    ) -> None:
        """Raise unless *user* may log time in *project*, against *issue_id* when given.

        - ``NotFoundError`` (404) for a project the user cannot reach, and for
          an issue that does not exist or that the user cannot see;
        - ``PermissionDeniedError`` (403) without ``log_time`` there;
        - ``ValidationError`` (422) for an issue of another project.

        Every way of writing a time entry on a user's behalf goes through it:
        the REST and MCP create, and starting and stopping a timer.
        """
        from specivo.services.issue_service import IssueService
        from specivo.services.permission_service import Permission, check_permission
        from specivo.services.project_service import ProjectService

        await ProjectService().require_project_access(session, project, user)
        if not await check_permission(user, project.id, Permission.LOG_TIME, session, request=request):
            raise PermissionDeniedError(f"Permission '{Permission.LOG_TIME}' denied for this project")
        if issue_id is not None:
            issue = await IssueService().get_by_id(session, issue_id, user=user)
            if issue.project_id != project.id:
                raise ValidationError(message="The issue belongs to another project", field="issue_id")

    async def can_edit(self, session: AsyncSession, user: User, entry: TimeEntry) -> bool:
        """Return whether *user* may edit or delete *entry*.

        Admins and holders of ``manage_time_entries`` may change any entry in
        the project; anyone else only their own, and only while they hold
        ``log_time`` there. Whether the entry is visible at all is checked
        first by ``get_visible``.
        """
        from specivo.services.permission_service import Permission, check_permission

        if admin_reaches(user, entry.project_id):
            return True
        if await check_permission(user, entry.project_id, Permission.MANAGE_TIME_ENTRIES, session):
            return True
        return entry.user_id == user.id and await check_permission(user, entry.project_id, Permission.LOG_TIME, session)

    async def list_for_issue(
        self, session: AsyncSession, issue_id: int, project: Project, user: User
    ) -> list[TimeEntry]:
        """Return the time entries logged on *issue_id* that *user* may read, newest first."""
        stmt = (
            select(TimeEntry)
            .where(TimeEntry.issue_id == issue_id)
            .options(selectinload(TimeEntry.user), selectinload(TimeEntry.activity))
            .order_by(TimeEntry.spent_on.desc(), TimeEntry.id.desc())
        )
        clause = await self.visible_entries_clause(session, user, project)
        if clause is not None:
            stmt = stmt.where(clause)
        return list((await session.execute(stmt)).scalars().all())

    async def sum_hours_for_issues(
        self, session: AsyncSession, issue_ids: list[int], project: Project, user: User
    ) -> Decimal:
        """Return the hours logged on *issue_ids* (all in *project*) that *user* may read."""
        if not issue_ids:
            return Decimal(0)
        stmt = select(func.coalesce(func.sum(TimeEntry.hours), 0)).where(TimeEntry.issue_id.in_(issue_ids))
        clause = await self.visible_entries_clause(session, user, project)
        if clause is not None:
            stmt = stmt.where(clause)
        return Decimal(str((await session.execute(stmt)).scalar_one()))

    # -------------------------------------------------------------------
    # Time entry CRUD
    # -------------------------------------------------------------------

    async def create(
        self,
        session: AsyncSession,
        project_id: int,
        data: TimeEntryCreate,
        user: User,
    ) -> TimeEntry:
        """Create a time entry."""
        # Validate activity exists and is active
        activity = await session.get(TimeEntryActivity, data.activity_id)
        if activity is None or not activity.active:
            raise ValidationError(
                message=f"Activity {data.activity_id} not found or inactive",
                field="activity_id",
            )

        entry = TimeEntry(
            project_id=project_id,
            issue_id=data.issue_id,
            user_id=user.id,
            activity_id=data.activity_id,
            hours=data.hours,
            comments=data.comments,
            spent_on=data.spent_on,
            is_billable=data.is_billable,
        )
        session.add(entry)
        await session.flush()
        await session.refresh(entry)
        return entry

    async def get_by_id(self, session: AsyncSession, entry_id: int) -> TimeEntry:
        """Get a time entry by ID with relationships loaded."""
        stmt = (
            select(TimeEntry)
            .where(TimeEntry.id == entry_id)
            .options(
                selectinload(TimeEntry.user),
                selectinload(TimeEntry.activity),
            )
        )
        result = await session.execute(stmt)
        entry = result.scalar_one_or_none()
        if entry is None:
            raise NotFoundError(f"Time entry {entry_id} not found")
        return entry

    async def list_for_project(
        self,
        session: AsyncSession,
        project: Project,
        viewer: User,
        *,
        user_id: int | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        offset: int = 0,
        limit: int = 25,
    ) -> tuple[list[TimeEntry], int]:
        """List the time entries of *project* that *viewer* may read, with optional filters."""
        base = select(TimeEntry).where(TimeEntry.project_id == project.id)
        count_base = select(func.count()).select_from(TimeEntry).where(TimeEntry.project_id == project.id)

        visible = await self.visible_entries_clause(session, viewer, project)
        if visible is not None:
            base = base.where(visible)
            count_base = count_base.where(visible)

        if user_id is not None:
            base = base.where(TimeEntry.user_id == user_id)
            count_base = count_base.where(TimeEntry.user_id == user_id)

        if from_date is not None:
            base = base.where(TimeEntry.spent_on >= from_date)
            count_base = count_base.where(TimeEntry.spent_on >= from_date)

        if to_date is not None:
            base = base.where(TimeEntry.spent_on <= to_date)
            count_base = count_base.where(TimeEntry.spent_on <= to_date)

        total = (await session.execute(count_base)).scalar_one()

        stmt = (
            base.options(
                selectinload(TimeEntry.user),
                selectinload(TimeEntry.activity),
            )
            .order_by(TimeEntry.spent_on.desc(), TimeEntry.id.desc())
            .offset(offset)
            .limit(limit)
        )
        entries = (await session.execute(stmt)).scalars().all()
        return list(entries), total

    async def update(
        self,
        session: AsyncSession,
        entry: TimeEntry,
        data: TimeEntryUpdate,
        user: User,
    ) -> TimeEntry:
        """Update a time entry; see ``can_edit`` for who may."""
        if not await self.can_edit(session, user, entry):
            raise PermissionDeniedError("You do not have permission to edit this time entry")

        if data.activity_id is not None:
            activity = await session.get(TimeEntryActivity, data.activity_id)
            if activity is None or not activity.active:
                raise ValidationError(
                    message=f"Activity {data.activity_id} not found or inactive",
                    field="activity_id",
                )
            entry.activity_id = data.activity_id

        if data.hours is not None:
            entry.hours = data.hours
        if data.comments is not None:
            entry.comments = data.comments
        if data.spent_on is not None:
            entry.spent_on = data.spent_on
        if data.is_billable is not None:
            entry.is_billable = data.is_billable

        session.add(entry)
        await session.flush()
        await session.refresh(entry)
        return entry

    async def delete(
        self,
        session: AsyncSession,
        entry: TimeEntry,
        user: User,
    ) -> None:
        """Delete a time entry; see ``can_edit`` for who may."""
        if not await self.can_edit(session, user, entry):
            raise PermissionDeniedError("You do not have permission to delete this time entry")

        await session.delete(entry)
        await session.flush()

    # -------------------------------------------------------------------
    # Activities
    # -------------------------------------------------------------------

    async def list_activities(self, session: AsyncSession) -> list[TimeEntryActivity]:
        """List all active time entry activities."""
        stmt = select(TimeEntryActivity).where(TimeEntryActivity.active.is_(True)).order_by(TimeEntryActivity.position)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    # -------------------------------------------------------------------
    # Timer operations
    # -------------------------------------------------------------------

    async def get_current_timer(self, session: AsyncSession, user: User) -> ActiveTimer | None:
        """Get the current active timer for the user.

        A timer running in a project outside the authenticating API key's
        project limit is not reported.
        """
        timer = await self._get_timer_row(session, user)
        if timer is not None and not project_in_scope(timer.project_id):
            return None
        return timer

    async def _get_timer_row(self, session: AsyncSession, user: User) -> ActiveTimer | None:
        stmt = select(ActiveTimer).where(ActiveTimer.user_id == user.id)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def start_timer(
        self,
        session: AsyncSession,
        user: User,
        project_id: int,
        issue_id: int | None = None,
        comments: str | None = None,
        *,
        request: Request | None = None,
    ) -> ActiveTimer:
        """Start a timer on *project_id* (and *issue_id*). Auto-stops existing timer if any.

        The user must be able to log time there (``require_can_log``). A
        running timer is stopped first and its time logged, unless the user
        may no longer log time where it runs; then it is discarded.
        """
        project = await session.get(Project, project_id)
        if project is None:
            raise NotFoundError(f"Project {project_id} not found")
        await self.require_can_log(session, user, project, issue_id, request=request)

        existing = await self._get_timer_row(session, user)
        if existing is not None and not project_in_scope(existing.project_id):
            # Neither stop nor discard a timer this key cannot reach.
            raise PermissionDeniedError(
                "A timer is already running in a project this API key cannot reach; stop it there first."
            )
        if existing is not None:
            if await self._timer_still_allowed(session, user, existing):
                await self._stop_timer_internal(session, user, existing)
            else:
                await session.delete(existing)
                await session.flush()

        timer = ActiveTimer(
            user_id=user.id,
            project_id=project_id,
            issue_id=issue_id,
            started_at=utcnow(),
            comments=comments,
        )
        session.add(timer)
        await session.flush()
        await session.refresh(timer)
        return timer

    async def stop_timer(
        self,
        session: AsyncSession,
        user: User,
        activity_id: int,
        *,
        request: Request | None = None,
    ) -> TimeEntry:
        """Stop the current timer and create a time entry.

        Raises NotFoundError if no active timer. Permissions may have changed
        since the timer started, so they are checked again before anything is
        written: when the user may no longer log time on the timer's project
        or issue, the timer is discarded (and that is committed) and the
        refusal is raised, so a timer that cannot be logged never lingers.
        """
        timer = await self.get_current_timer(session, user)
        if timer is None:
            raise NotFoundError("No active timer")

        # Validate activity
        activity = await session.get(TimeEntryActivity, activity_id)
        if activity is None or not activity.active:
            raise ValidationError(
                message=f"Activity {activity_id} not found or inactive",
                field="activity_id",
            )

        try:
            await self._require_timer_allowed(session, user, timer, request=request)
        except AppError as exc:
            await session.delete(timer)
            await session.commit()
            exc.message = f"{exc.message}. The timer was discarded without logging time."
            raise

        return await self._stop_timer_internal(session, user, timer, activity_id=activity_id)

    async def _require_timer_allowed(
        self, session: AsyncSession, user: User, timer: ActiveTimer, *, request: Request | None = None
    ) -> None:
        """Raise unless *user* may still log time where *timer* runs."""
        project = await session.get(Project, timer.project_id)
        if project is None:
            raise NotFoundError(f"Project {timer.project_id} not found")
        await self.require_can_log(session, user, project, timer.issue_id, request=request)

    async def _timer_still_allowed(self, session: AsyncSession, user: User, timer: ActiveTimer) -> bool:
        try:
            await self._require_timer_allowed(session, user, timer)
        except AppError:
            return False
        return True

    async def default_activity(self, session: AsyncSession) -> TimeEntryActivity | None:
        """The active activity marked default, else the first active one by position; None when none is active."""
        stmt = select(TimeEntryActivity).where(
            TimeEntryActivity.is_default.is_(True),
            TimeEntryActivity.active.is_(True),
        )
        activity = (await session.execute(stmt)).scalars().first()
        if activity is not None:
            return activity
        stmt = (
            select(TimeEntryActivity)
            .where(TimeEntryActivity.active.is_(True))
            .order_by(TimeEntryActivity.position, TimeEntryActivity.id)
            .limit(1)
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    async def _stop_timer_internal(
        self,
        session: AsyncSession,
        user: User,
        timer: ActiveTimer,
        activity_id: int | None = None,
    ) -> TimeEntry:
        """Internal: stop a timer, create time entry, delete timer."""
        now = utcnow()
        hours = _compute_elapsed_hours(timer.started_at, now)

        # If no activity_id provided (auto-stop), use default activity
        if activity_id is None:
            default_activity = await self.default_activity(session)
            if default_activity is None:
                raise ValidationError(
                    message="No active activities available",
                    field="activity_id",
                )
            activity_id = default_activity.id

        comments = timer.comments
        if hours == _get_max_timer_hours():
            cap_note = " [auto-capped at 12h]"
            comments = f"{comments}{cap_note}" if comments else cap_note.strip()

        entry = TimeEntry(
            project_id=timer.project_id,
            issue_id=timer.issue_id,
            user_id=user.id,
            activity_id=activity_id,
            hours=hours,
            comments=comments,
            spent_on=now.date(),
            is_billable=False,
        )
        session.add(entry)

        # Delete the timer
        await session.delete(timer)
        await session.flush()
        await session.refresh(entry)
        return entry
