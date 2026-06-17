"""Recurring patterns API — project-scoped CRUD, occurrence preview, and edit-scope.

A recurring pattern is a project-owned template that spawns issues on a schedule.
These endpoints mirror the Versions router: project-scoped paths, the shared
``_get_project`` / ``require_project_access`` helpers, and a
``_require_manage_recurring_tasks`` permission gate for mutating operations.

Read operations (list, detail, occurrences preview) require ``view_issues``;
mutating operations (create, update, delete, skip, override, split) require
``manage_recurring_tasks``.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.core.database import get_db
from specivo.core.exceptions import NotFoundError, PermissionDeniedError
from specivo.core.security import get_current_user
from specivo.core.utils import utcnow
from specivo.models.project import Project
from specivo.models.recurring_pattern import RecurringPattern
from specivo.models.user import User
from specivo.schemas.recurring_pattern import (
    OccurrencePreview,
    OverrideOccurrenceRequest,
    RecurrenceExceptionOut,
    RecurringPatternCreate,
    RecurringPatternOut,
    RecurringPatternUpdate,
    SkipOccurrenceRequest,
    SplitFromRequest,
)
from specivo.services.permission_service import Permission, check_permission
from specivo.services.project_service import ProjectService
from specivo.services.recurrence import expand_occurrences
from specivo.services.recurring_pattern_service import RecurringPatternService

router = APIRouter(tags=["recurring-patterns"])
_project_service = ProjectService()
_pattern_service = RecurringPatternService()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _get_project(project_key: str, user: User, db: AsyncSession) -> Project:
    """Resolve a project by key; raises NotFoundError if missing or inaccessible."""
    project = await _project_service.get_by_key(db, project_key.upper())
    await _project_service.require_project_access(db, project, user)
    return project


async def _require_view_issues(project: Project, user: User, db: AsyncSession) -> None:
    """Raise 403 if user lacks view_issues on *project*."""
    if user.is_admin:
        return
    allowed = await check_permission(user, project.id, Permission.VIEW_ISSUES, db)
    if not allowed:
        raise PermissionDeniedError("You do not have permission to view issues")


async def _require_manage_recurring_tasks(
    project: Project,
    user: User,
    db: AsyncSession,
) -> None:
    """Raise 403 if user lacks manage_recurring_tasks on *project*."""
    if user.is_admin:
        return
    allowed = await check_permission(user, project.id, Permission.MANAGE_RECURRING_TASKS, db)
    if not allowed:
        raise PermissionDeniedError("You do not have permission to manage recurring tasks")


async def _get_pattern_in_project(
    pattern_id: int, project: Project, db: AsyncSession
) -> RecurringPattern:
    """Load a pattern and assert it belongs to *project* (404 otherwise)."""
    pattern = await _pattern_service.get_by_id(db, pattern_id)
    if pattern.project_id != project.id:
        raise NotFoundError(
            f"Recurring pattern {pattern_id} not found in project '{project.key}'"
        )
    return pattern


def _pattern_out(pattern: RecurringPattern, project_key: str) -> RecurringPatternOut:
    # project_key is derived (not an ORM column). Stash it on the instance so
    # the ``from_attributes`` validator can read it like any other field; the
    # attribute is transient and never persisted.
    pattern.project_key = project_key  # type: ignore[attr-defined]
    return RecurringPatternOut.model_validate(pattern)


# ---------------------------------------------------------------------------
# CRUD endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/projects/{project_key}/recurring-patterns/",
    response_model=list[RecurringPatternOut],
)
async def list_recurring_patterns(
    project_key: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[RecurringPatternOut]:
    project = await _get_project(project_key, current_user, db)
    await _require_view_issues(project, current_user, db)
    patterns = await _pattern_service.list_for_project(db, project.id)
    return [_pattern_out(p, project.key) for p in patterns]


@router.post(
    "/projects/{project_key}/recurring-patterns/",
    response_model=RecurringPatternOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_recurring_pattern(
    project_key: str,
    data: RecurringPatternCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RecurringPatternOut:
    project = await _get_project(project_key, current_user, db)
    await _require_manage_recurring_tasks(project, current_user, db)
    pattern = await _pattern_service.create(db, project, data, current_user)
    return _pattern_out(pattern, project.key)


@router.get(
    "/projects/{project_key}/recurring-patterns/{pattern_id}/",
    response_model=RecurringPatternOut,
)
async def get_recurring_pattern(
    project_key: str,
    pattern_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RecurringPatternOut:
    project = await _get_project(project_key, current_user, db)
    await _require_view_issues(project, current_user, db)
    pattern = await _get_pattern_in_project(pattern_id, project, db)
    return _pattern_out(pattern, project.key)


@router.patch(
    "/projects/{project_key}/recurring-patterns/{pattern_id}/",
    response_model=RecurringPatternOut,
)
async def update_recurring_pattern(
    project_key: str,
    pattern_id: int,
    data: RecurringPatternUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RecurringPatternOut:
    project = await _get_project(project_key, current_user, db)
    await _require_manage_recurring_tasks(project, current_user, db)
    pattern = await _get_pattern_in_project(pattern_id, project, db)
    pattern = await _pattern_service.update(db, pattern, data)
    # ``updated_at`` is refreshed server-side (onupdate=now()); reload so the
    # response serialiser does not trigger lazy IO in the async sync boundary.
    await db.refresh(pattern)
    return _pattern_out(pattern, project.key)


@router.delete(
    "/projects/{project_key}/recurring-patterns/{pattern_id}/",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_recurring_pattern(
    project_key: str,
    pattern_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    project = await _get_project(project_key, current_user, db)
    await _require_manage_recurring_tasks(project, current_user, db)
    pattern = await _get_pattern_in_project(pattern_id, project, db)
    await _pattern_service.delete(db, pattern)


# ---------------------------------------------------------------------------
# Occurrence preview (DB-free, via the engine)
# ---------------------------------------------------------------------------


@router.get(
    "/projects/{project_key}/recurring-patterns/{pattern_id}/occurrences/",
    response_model=OccurrencePreview,
)
async def preview_occurrences(
    project_key: str,
    pattern_id: int,
    days: int | None = Query(
        default=None,
        ge=1,
        description=(
            "Look-ahead window in days from now. Defaults to the pattern's "
            "creation_lead_time_days; capped at the server's configured "
            "recurring_tasks_max_lead_time_days."
        ),
    ),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OccurrencePreview:
    """Preview upcoming occurrences without materialising any issues.

    The window runs from ``now`` to ``now + days`` (UTC). ``days`` defaults to
    the pattern's ``creation_lead_time_days`` and is capped at the server's
    ``recurring_tasks_max_lead_time_days`` so a request can never expand an
    unbounded series. The pattern's skip exceptions are loaded and passed as
    EXDATEs, so the preview matches what generation would actually produce.
    """
    project = await _get_project(project_key, current_user, db)
    await _require_view_issues(project, current_user, db)
    pattern = await _get_pattern_in_project(pattern_id, project, db)

    settings = get_settings()
    window_days = days if days is not None else pattern.creation_lead_time_days
    window_days = min(window_days, settings.recurring_tasks_max_lead_time_days)

    now = utcnow()
    window_end = now + timedelta(days=window_days)

    # Load skip exceptions (EXDATEs) so the preview reflects reality.
    exdates, _overrides = await _pattern_service._load_exceptions(db, pattern.id)

    occurrences = expand_occurrences(
        _pattern_service.build_spec(pattern), now, window_end, exdates
    )
    return OccurrencePreview(occurrences=occurrences, count=len(occurrences))


# ---------------------------------------------------------------------------
# Edit-scope endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/projects/{project_key}/recurring-patterns/{pattern_id}/skip/",
    response_model=RecurrenceExceptionOut,
)
async def skip_occurrence(
    project_key: str,
    pattern_id: int,
    body: SkipOccurrenceRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RecurrenceExceptionOut:
    project = await _get_project(project_key, current_user, db)
    await _require_manage_recurring_tasks(project, current_user, db)
    pattern = await _get_pattern_in_project(pattern_id, project, db)
    exc = await _pattern_service.skip_occurrence(db, pattern, body.occurrence_at)
    return RecurrenceExceptionOut.model_validate(exc)


@router.post(
    "/projects/{project_key}/recurring-patterns/{pattern_id}/override/",
    response_model=RecurrenceExceptionOut,
)
async def override_occurrence(
    project_key: str,
    pattern_id: int,
    body: OverrideOccurrenceRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RecurrenceExceptionOut:
    project = await _get_project(project_key, current_user, db)
    await _require_manage_recurring_tasks(project, current_user, db)
    pattern = await _get_pattern_in_project(pattern_id, project, db)
    exc = await _pattern_service.override_occurrence(
        db, pattern, body.occurrence_at, body.payload
    )
    return RecurrenceExceptionOut.model_validate(exc)


@router.post(
    "/projects/{project_key}/recurring-patterns/{pattern_id}/split/",
    response_model=RecurringPatternOut,
    status_code=status.HTTP_201_CREATED,
)
async def split_recurring_pattern(
    project_key: str,
    pattern_id: int,
    body: SplitFromRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RecurringPatternOut:
    """Split a series this-and-future: terminate the old one, create a new one.

    Returns the newly created (future) pattern.
    """
    project = await _get_project(project_key, current_user, db)
    await _require_manage_recurring_tasks(project, current_user, db)
    pattern = await _get_pattern_in_project(pattern_id, project, db)
    new_pattern = await _pattern_service.split_from(
        db, pattern, body.occurrence_at, body.new_pattern
    )
    # Reload so server-defaulted timestamps are populated before serialising.
    await db.refresh(new_pattern)
    return _pattern_out(new_pattern, project.key)
