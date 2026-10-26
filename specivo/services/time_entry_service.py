"""Time entry service — CRUD for time entries and timer operations.

Which time entries a user may read is decided here, in
``visible_entries_clause`` (one project) and ``visible_entries_clause_any``
(across projects), from the roles ``permission_service.get_user_roles``
resolves. Every reader goes through one of them: the project list (web and
REST), a single entry, the issue's time tab and its logged-time sum, a
version's logged-time sum, the billing report and the dashboard's agent feed.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.sql.elements import ColumnElement

from specivo.core.config import get_settings
from specivo.core.exceptions import NotFoundError, PermissionDeniedError, ValidationError
from specivo.core.utils import utcnow
from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.role import TIME_ENTRIES_VISIBILITY_OWN, Role
from specivo.models.time_entry import ActiveTimer, TimeEntry, TimeEntryActivity
from specivo.models.user import User
from specivo.schemas.time_entry import TimeEntryCreate, TimeEntryUpdate

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

        None means every entry (admins, or a role with
        ``time_entries_visibility = 'all'``); otherwise only the entries the
        user logged. Reaching the project is the caller's job
        (``ProjectService.require_project_access``).
        """
        from specivo.services.permission_service import time_entries_visibility

        if await time_entries_visibility(session, user, project) == TIME_ENTRIES_VISIBILITY_OWN:
            return TimeEntry.user_id == user.id
        return None

    async def visible_entries_clause_any(self, session: AsyncSession, user: User) -> ColumnElement[bool] | None:
        """Return the cross-project form of ``visible_entries_clause``, or None for no limit.

        Mirrors ``get_user_roles`` + ``best_time_entries_visibility`` in SQL,
        the way ``IssueService.visible_issues_clause`` does for issues: a
        project is restricted to the user's own entries when the user's
        membership roles there are all ``'own'``, or, on a public project
        without a membership, when the Non member role is. It narrows only by
        visibility; callers still limit the query to projects the user can
        reach.
        """
        if user.is_admin:
            return None
        if user.is_anonymous:
            return TimeEntry.user_id == user.id

        from specivo.services.permission_service import (
            best_time_entries_visibility,
            get_non_member_role,
            member_principal_clause,
        )

        rows = await session.execute(
            select(Member.project_id, Role.time_entries_visibility)
            .select_from(Member)
            .outerjoin(MemberRole, MemberRole.member_id == Member.id)
            .outerjoin(Role, Role.id == MemberRole.role_id)
            .where(member_principal_clause(user.id))
        )
        by_project: dict[int, set[str]] = {}
        for project_id, visibility in rows:
            values = by_project.setdefault(project_id, set())
            if visibility is not None:
                values.add(visibility)

        restricted = sorted(
            project_id
            for project_id, values in by_project.items()
            if best_time_entries_visibility(values) == TIME_ENTRIES_VISIBILITY_OWN
        )

        visible: list[ColumnElement[bool]] = [TimeEntry.user_id == user.id]
        unrestricted: ColumnElement[bool] | None = TimeEntry.project_id.not_in(restricted) if restricted else None

        non_member = await get_non_member_role(session)
        if non_member is not None and non_member.time_entries_visibility == TIME_ENTRIES_VISIBILITY_OWN:
            public_without_membership = select(Project.id).where(Project.is_public.is_(True))
            if by_project:
                public_without_membership = public_without_membership.where(Project.id.not_in(sorted(by_project)))
            outside = TimeEntry.project_id.not_in(public_without_membership)
            unrestricted = outside if unrestricted is None else and_(unrestricted, outside)

        if unrestricted is None:
            return None
        visible.append(unrestricted)
        return or_(*visible)

    async def get_visible(self, session: AsyncSession, entry_id: int, user: User) -> TimeEntry:
        """Return time entry *entry_id* if *user* may read it, else raise ``NotFoundError``.

        404 rather than 403 for an entry in an unreachable project or outside
        the user's visibility, so entry ids cannot be probed.
        """
        from specivo.services.project_service import ProjectService

        entry = await self.get_by_id(session, entry_id)
        project = await session.get(Project, entry.project_id)
        if project is None:
            raise NotFoundError(f"Time entry {entry_id} not found")
        try:
            await ProjectService().require_project_access(session, project, user)
        except NotFoundError:
            raise NotFoundError(f"Time entry {entry_id} not found") from None
        clause = await self.visible_entries_clause(session, user, project)
        if clause is not None and entry.user_id != user.id:
            raise NotFoundError(f"Time entry {entry_id} not found")
        return entry

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
        """Update a time entry. Owner or admin can update."""
        if entry.user_id != user.id and not user.is_admin:
            raise PermissionDeniedError("You can only edit your own time entries")

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
        """Delete a time entry. Owner or admin can delete."""
        if entry.user_id != user.id and not user.is_admin:
            raise PermissionDeniedError("You can only delete your own time entries")

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
        """Get the current active timer for the user."""
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
    ) -> ActiveTimer:
        """Start a timer. Auto-stops existing timer if any."""
        existing = await self.get_current_timer(session, user)
        if existing is not None:
            # Auto-stop the existing timer, creating a time entry
            await self._stop_timer_internal(session, user, existing)

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
    ) -> TimeEntry:
        """Stop the current timer and create a time entry.

        Raises NotFoundError if no active timer.
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

        return await self._stop_timer_internal(session, user, timer, activity_id=activity_id)

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
            stmt = select(TimeEntryActivity).where(
                TimeEntryActivity.is_default.is_(True),
                TimeEntryActivity.active.is_(True),
            )
            result = await session.execute(stmt)
            default_activity = result.scalar_one_or_none()
            if default_activity is not None:
                activity_id = default_activity.id
            else:
                # Fallback to first active activity
                stmt2 = (
                    select(TimeEntryActivity)
                    .where(TimeEntryActivity.active.is_(True))
                    .order_by(TimeEntryActivity.position)
                    .limit(1)
                )
                result2 = await session.execute(stmt2)
                fallback = result2.scalar_one_or_none()
                if fallback is not None:
                    activity_id = fallback.id
                else:
                    raise ValidationError(
                        message="No active activities available",
                        field="activity_id",
                    )

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
