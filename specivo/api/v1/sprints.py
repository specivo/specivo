"""Sprints API — CRUD, lifecycle, board, and backlog endpoints scoped to a project."""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import get_db
from specivo.core.exceptions import NotFoundError, PermissionDeniedError
from specivo.core.security import get_current_user
from specivo.models.project import Project
from specivo.models.user import User
from specivo.schemas.sprint import BurndownOut, SprintCompleteRequest, SprintCreate, SprintOut, SprintUpdate
from specivo.services.permission_service import Permission, check_permission
from specivo.services.project_service import ProjectService
from specivo.services.sprint_service import SprintService

router = APIRouter(tags=["sprints"])
_project_service = ProjectService()
_sprint_service = SprintService()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _get_project(project_key: str, user: User, db: AsyncSession) -> Project:
    """Resolve a project by key; raises NotFoundError if missing or inaccessible."""
    project = await _project_service.get_by_key(db, project_key.upper())
    await _project_service.require_project_access(db, project, user)
    return project


async def _require_manage_sprints(
    project: Project,
    user: User,
    db: AsyncSession,
) -> None:
    """Raise 403 if user lacks manage_sprints on *project*."""
    if user.is_admin:
        return
    allowed = await check_permission(user, project.id, Permission.MANAGE_SPRINTS, db)
    if not allowed:
        raise PermissionDeniedError("You do not have permission to manage sprints")


async def _require_view_issues(
    project: Project,
    user: User,
    db: AsyncSession,
) -> None:
    """Raise 403 if user lacks view_issues on *project*."""
    if user.is_admin:
        return
    allowed = await check_permission(user, project.id, Permission.VIEW_ISSUES, db)
    if not allowed:
        raise PermissionDeniedError("You do not have permission to view issues")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/projects/{project_key}/sprints/",
    response_model=SprintOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_sprint(
    project_key: str,
    data: SprintCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SprintOut:
    project = await _get_project(project_key, current_user, db)
    await _require_manage_sprints(project, current_user, db)
    sprint = await _sprint_service.create(db, project, data)
    return SprintOut.model_validate(sprint)


@router.get(
    "/projects/{project_key}/sprints/",
    response_model=list[SprintOut],
)
async def list_sprints(
    project_key: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[SprintOut]:
    project = await _get_project(project_key, current_user, db)
    await _require_view_issues(project, current_user, db)
    sprints = await _sprint_service.list_for_project(db, project.id)
    return [SprintOut.model_validate(s) for s in sprints]


@router.get(
    "/projects/{project_key}/sprints/{sprint_id}/burndown/",
    response_model=BurndownOut,
)
async def get_burndown(
    project_key: str,
    sprint_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BurndownOut:
    project = await _get_project(project_key, current_user, db)
    await _require_view_issues(project, current_user, db)
    sprint = await _sprint_service.get_by_id(db, sprint_id)
    if sprint.project_id != project.id:
        raise NotFoundError(f"Sprint {sprint_id} not found in project '{project.key}'")
    data = await _sprint_service.burndown_data(db, sprint)
    return BurndownOut(
        total_estimated_hours=data["total_estimated_hours"],
        completed_hours=data["completed_hours"],
        data_points=[
            {
                "date": p["date"],
                "remaining_hours": p["remaining"],
                "ideal_remaining": p["ideal"],
            }
            for p in data["data_points"]
        ],
    )


@router.get(
    "/projects/{project_key}/sprints/{sprint_id}/",
    response_model=SprintOut,
)
async def get_sprint(
    project_key: str,
    sprint_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SprintOut:
    project = await _get_project(project_key, current_user, db)
    await _require_view_issues(project, current_user, db)
    sprint = await _sprint_service.get_by_id(db, sprint_id)
    if sprint.project_id != project.id:
        raise NotFoundError(f"Sprint {sprint_id} not found in project '{project.key}'")
    return SprintOut.model_validate(sprint)


@router.patch(
    "/projects/{project_key}/sprints/{sprint_id}/",
    response_model=SprintOut,
)
async def update_sprint(
    project_key: str,
    sprint_id: int,
    data: SprintUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SprintOut:
    project = await _get_project(project_key, current_user, db)
    await _require_manage_sprints(project, current_user, db)
    sprint = await _sprint_service.get_by_id(db, sprint_id)
    if sprint.project_id != project.id:
        raise NotFoundError(f"Sprint {sprint_id} not found in project '{project.key}'")
    sprint = await _sprint_service.update(db, sprint, data)
    return SprintOut.model_validate(sprint)


@router.delete(
    "/projects/{project_key}/sprints/{sprint_id}/",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_sprint(
    project_key: str,
    sprint_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    project = await _get_project(project_key, current_user, db)
    await _require_manage_sprints(project, current_user, db)
    sprint = await _sprint_service.get_by_id(db, sprint_id)
    if sprint.project_id != project.id:
        raise NotFoundError(f"Sprint {sprint_id} not found in project '{project.key}'")
    await _sprint_service.delete(db, sprint)


@router.post(
    "/projects/{project_key}/sprints/{sprint_id}/start/",
    response_model=SprintOut,
)
async def start_sprint(
    project_key: str,
    sprint_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SprintOut:
    project = await _get_project(project_key, current_user, db)
    await _require_manage_sprints(project, current_user, db)
    sprint = await _sprint_service.get_by_id(db, sprint_id)
    if sprint.project_id != project.id:
        raise NotFoundError(f"Sprint {sprint_id} not found in project '{project.key}'")
    sprint = await _sprint_service.start_sprint(db, sprint)
    return SprintOut.model_validate(sprint)


@router.post(
    "/projects/{project_key}/sprints/{sprint_id}/complete/",
    response_model=SprintOut,
)
async def complete_sprint(
    project_key: str,
    sprint_id: int,
    data: SprintCompleteRequest | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SprintOut:
    project = await _get_project(project_key, current_user, db)
    await _require_manage_sprints(project, current_user, db)
    sprint = await _sprint_service.get_by_id(db, sprint_id)
    if sprint.project_id != project.id:
        raise NotFoundError(f"Sprint {sprint_id} not found in project '{project.key}'")
    move_to = data.move_incomplete_to_sprint_id if data else None
    sprint = await _sprint_service.complete_sprint(db, sprint, move_to_sprint_id=move_to)
    return SprintOut.model_validate(sprint)


@router.get(
    "/projects/{project_key}/sprints/{sprint_id}/board/",
)
async def get_board(
    project_key: str,
    sprint_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    project = await _get_project(project_key, current_user, db)
    await _require_view_issues(project, current_user, db)
    sprint = await _sprint_service.get_by_id(db, sprint_id)
    if sprint.project_id != project.id:
        raise NotFoundError(f"Sprint {sprint_id} not found in project '{project.key}'")
    board = await _sprint_service.board_data(db, sprint)
    # Serialize issues to dicts for JSON response
    result: dict[str, list[dict]] = {}
    for status_name, issues in board.items():
        result[status_name] = [
            {"id": i.id, "subject": i.subject, "status_id": i.status_id}
            for i in issues
        ]
    return result


@router.get(
    "/projects/{project_key}/backlog/",
)
async def get_backlog(
    project_key: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    project = await _get_project(project_key, current_user, db)
    await _require_view_issues(project, current_user, db)
    issues, _total = await _sprint_service.backlog_issues(db, project.id)
    return [
        {"id": i.id, "subject": i.subject, "sprint_id": i.sprint_id}
        for i in issues
    ]
