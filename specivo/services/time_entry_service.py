"""Time entry service — CRUD for time entries and timer operations."""

from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from specivo.core.config import get_settings
from specivo.core.exceptions import NotFoundError, PermissionDeniedError, ValidationError
from specivo.core.utils import utcnow
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
        project_id: int,
        *,
        user_id: int | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        offset: int = 0,
        limit: int = 25,
    ) -> tuple[list[TimeEntry], int]:
        """List time entries for a project with optional filters."""
        base = select(TimeEntry).where(TimeEntry.project_id == project_id)
        count_base = select(func.count()).select_from(TimeEntry).where(TimeEntry.project_id == project_id)

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
