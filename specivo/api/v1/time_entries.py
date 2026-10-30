"""Time Entries API — CRUD, activities, timer operations."""

from __future__ import annotations

import logging
from datetime import date

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import DB_SESSION
from specivo.core.exceptions import PermissionDeniedError
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
from specivo.services.permission_service import Permission, check_permission
from specivo.services.project_service import ProjectService
from specivo.services.security_audit_service import AuditEvent, SecurityAuditService
from specivo.services.time_entry_service import TimeEntryService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["time-entries"])
_service = TimeEntryService()
_project_service = ProjectService()
_audit = SecurityAuditService()


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
    db: AsyncSession = DB_SESSION,
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
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = DB_SESSION,
) -> TimeEntryOut:
    """Create a time entry in the given project.

    Requires ``log_time`` there (403), and an ``issue_id`` must name an issue
    of that project the caller can see.
    """
    project = await _project_service.get_by_key(db, project_key.upper())
    await _service.require_can_log(db, current_user, project, data.issue_id, request=request)
    entry = await _service.create(db, project.id, data, current_user)
    entry = await _service.get_by_id(db, entry.id)
    return _entry_out(entry)


@router.get(
    "/projects/{project_key}/time-entries/",
    response_model=TimeEntryListResponse,
)
async def list_time_entries(
    project_key: str,
    request: Request,
    user_id: int | None = Query(default=None),
    from_date: date | None = Query(default=None),
    to_date: date | None = Query(default=None),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=25, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = DB_SESSION,
) -> TimeEntryListResponse:
    """List the project's time entries the caller may read, with optional filters.

    Requires ``view_time_entries`` (403 without it). When every role granting
    it has ``time_entries_visibility = 'own'``, the list holds only the
    caller's own entries.
    """
    project = await _project_service.get_by_key(db, project_key.upper())
    await _project_service.require_project_access(db, project, current_user)
    if not await check_permission(current_user, project.id, Permission.VIEW_TIME_ENTRIES, db, request=request):
        raise PermissionDeniedError("You do not have permission to view time entries in this project")
    entries, total = await _service.list_for_project(
        db,
        project,
        current_user,
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
    db: AsyncSession = DB_SESSION,
) -> TimeEntryOut:
    """Get a single time entry by ID; 404 unless the caller may read it."""
    entry = await _service.get_visible(db, entry_id, current_user)
    return _entry_out(entry)


@router.patch(
    "/time-entries/{entry_id}/",
    response_model=TimeEntryOut,
)
async def update_time_entry(
    entry_id: int,
    data: TimeEntryUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = DB_SESSION,
) -> TimeEntryOut:
    """Update a time entry: any with ``manage_time_entries``, else only one's own with ``log_time``.

    404 for an entry the caller cannot read, 403 for one they can read but not edit.
    """
    entry = await _service.get_visible(db, entry_id, current_user)
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
    db: AsyncSession = DB_SESSION,
) -> None:
    """Delete a time entry: any with ``manage_time_entries``, else only one's own with ``log_time``.

    404 for an entry the caller cannot read, 403 for one they can read but not delete.
    """
    entry = await _service.get_visible(db, entry_id, current_user)
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
    db: AsyncSession = DB_SESSION,
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
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = DB_SESSION,
) -> TimerOut:
    """Start a timer. Auto-stops existing timer if any.

    Requires ``log_time`` in the project (404 for a project or issue the
    caller cannot see, 403 without the permission).
    """
    timer = await _service.start_timer(
        db,
        current_user,
        project_id=data.project_id,
        issue_id=data.issue_id,
        comments=data.comments,
        request=request,
    )
    return _timer_out(timer)


@router.post(
    "/timer/stop/",
    response_model=TimeEntryOut,
    status_code=status.HTTP_201_CREATED,
)
async def stop_timer(
    data: TimerStopRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = DB_SESSION,
) -> TimeEntryOut:
    """Stop the current timer and create a time entry.

    Permissions are checked again: a timer the caller may no longer log is
    discarded and the request refused (404 or 403).
    """
    entry = await _service.stop_timer(db, current_user, data.activity_id, data.comments, request=request)
    entry = await _service.get_by_id(db, entry.id)
    await db.commit()  # commit before response to avoid reload race condition
    return _entry_out(entry)


@router.post(
    "/timer/discard/",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def discard_timer(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = DB_SESSION,
) -> None:
    """Discard the current timer without logging time; 404 when none runs.

    Only the caller's own timer is ever touched. The discard is recorded in
    the security audit log.
    """
    timer = await _service.discard_timer(db, current_user)
    elapsed = int((utcnow() - timer.started_at).total_seconds())
    try:
        await _audit.log_event(
            session=db,
            event_type=AuditEvent.TIMER_DISCARDED,
            user_id=current_user.id,
            project_id=timer.project_id,
            resource_type="active_timer",
            resource_id=timer.id,
            details={"issue_id": timer.issue_id, "elapsed_seconds": max(0, elapsed)},
            request=request,
        )
    except Exception:
        logger.warning("Audit write failed for a discarded timer", exc_info=True)
