"""Time Entries API — CRUD, activities, timer operations."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import get_db
from specivo.core.security import get_current_user
from specivo.core.utils import utcnow
from specivo.models.user import User
from specivo.schemas.common import IdName
from specivo.schemas.time_entry import (
    ActivityOut,
    TimeEntryCreate,
    TimeEntryListResponse,
    TimeEntryOut,
    TimeEntryUpdate,
    TimerOut,
    TimerStartRequest,
    TimerStopRequest,
)
from specivo.services.project_service import ProjectService
from specivo.services.time_entry_service import TimeEntryService

router = APIRouter(tags=["time-entries"])
_service = TimeEntryService()
_project_service = ProjectService()


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------


def _entry_out(entry) -> TimeEntryOut:
    """Build a TimeEntryOut from a TimeEntry with loaded relationships."""
    return TimeEntryOut(
        id=entry.id,
        project_id=entry.project_id,
        issue_id=entry.issue_id,
        user=IdName(id=entry.user_id, name=entry.user.display_name),
        activity=IdName(id=entry.activity_id, name=entry.activity.name),
        hours=entry.hours,
        comments=entry.comments,
        spent_on=entry.spent_on,
        is_billable=entry.is_billable,
        created_at=entry.created_at,
        updated_at=entry.updated_at,
    )


def _timer_out(timer) -> TimerOut:
    """Build a TimerOut from an ActiveTimer."""
    now = utcnow()
    elapsed = int((now - timer.started_at).total_seconds())
    return TimerOut(
        id=timer.id,
        user_id=timer.user_id,
        project_id=timer.project_id,
        issue_id=timer.issue_id,
        started_at=timer.started_at,
        comments=timer.comments,
        elapsed_seconds=max(0, elapsed),
    )


# ---------------------------------------------------------------------------
# Activities
# ---------------------------------------------------------------------------


@router.get(
    "/time-entries/activities/",
    response_model=list[ActivityOut],
)
async def list_activities(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ActivityOut]:
    """List all active time entry activities."""
    activities = await _service.list_activities(db)
    return [ActivityOut.model_validate(a) for a in activities]


# ---------------------------------------------------------------------------
# Project-scoped endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/projects/{project_key}/time-entries/",
    response_model=TimeEntryOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_time_entry(
    project_key: str,
    data: TimeEntryCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TimeEntryOut:
    """Create a time entry in the given project."""
    project = await _project_service.get_by_key(db, project_key.upper())
    entry = await _service.create(db, project.id, data, current_user)
    entry = await _service.get_by_id(db, entry.id)
    return _entry_out(entry)


@router.get(
    "/projects/{project_key}/time-entries/",
    response_model=TimeEntryListResponse,
)
async def list_time_entries(
    project_key: str,
    user_id: int | None = Query(default=None),
    from_date: date | None = Query(default=None),
    to_date: date | None = Query(default=None),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=25, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TimeEntryListResponse:
    """List time entries for a project with optional filters."""
    project = await _project_service.get_by_key(db, project_key.upper())
    entries, total = await _service.list_for_project(
        db,
        project.id,
        user_id=user_id,
        from_date=from_date,
        to_date=to_date,
        offset=offset,
        limit=limit,
    )
    return TimeEntryListResponse(
        total_count=total,
        offset=offset,
        limit=limit,
        items=[_entry_out(e) for e in entries],
    )


# ---------------------------------------------------------------------------
# Global time entry endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/time-entries/{entry_id}/",
    response_model=TimeEntryOut,
)
async def get_time_entry(
    entry_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TimeEntryOut:
    """Get a single time entry by ID."""
    entry = await _service.get_by_id(db, entry_id)
    return _entry_out(entry)


@router.patch(
    "/time-entries/{entry_id}/",
    response_model=TimeEntryOut,
)
async def update_time_entry(
    entry_id: int,
    data: TimeEntryUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TimeEntryOut:
    """Update a time entry (owner or admin)."""
    entry = await _service.get_by_id(db, entry_id)
    entry = await _service.update(db, entry, data, current_user)
    entry = await _service.get_by_id(db, entry.id)
    return _entry_out(entry)


@router.delete(
    "/time-entries/{entry_id}/",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_time_entry(
    entry_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a time entry (owner or admin)."""
    entry = await _service.get_by_id(db, entry_id)
    await _service.delete(db, entry, current_user)


# ---------------------------------------------------------------------------
# Timer endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/timer/",
    response_model=TimerOut | None,
)
async def get_timer(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TimerOut | None:
    """Get the current active timer for the authenticated user."""
    timer = await _service.get_current_timer(db, current_user)
    if timer is None:
        return None
    return _timer_out(timer)


@router.post(
    "/timer/start/",
    response_model=TimerOut,
    status_code=status.HTTP_201_CREATED,
)
async def start_timer(
    data: TimerStartRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TimerOut:
    """Start a timer. Auto-stops existing timer if any."""
    timer = await _service.start_timer(
        db,
        current_user,
        project_id=data.project_id,
        issue_id=data.issue_id,
        comments=data.comments,
    )
    return _timer_out(timer)


@router.post(
    "/timer/stop/",
    response_model=TimeEntryOut,
    status_code=status.HTTP_201_CREATED,
)
async def stop_timer(
    data: TimerStopRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TimeEntryOut:
    """Stop the current timer and create a time entry."""
    entry = await _service.stop_timer(db, current_user, data.activity_id)
    entry = await _service.get_by_id(db, entry.id)
    return _entry_out(entry)
