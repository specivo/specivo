"""Projects API — CRUD, membership, and module management."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import get_db
from specivo.core.exceptions import AppError, NotFoundError, PermissionDeniedError
from specivo.core.security import get_current_user
from specivo.models.member import Member
from specivo.models.user import User
from specivo.schemas.common import PaginatedResponse
from specivo.schemas.project import (
    MemberAdd,
    MemberOut,
    ModulesOut,
    ModuleToggle,
    ProjectCreate,
    ProjectOut,
    ProjectUpdate,
)
from specivo.services.permission_service import check_permission
from specivo.services.project_service import ProjectService

router = APIRouter(prefix="/projects", tags=["projects"])
_service = ProjectService()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _require_manage(
    project,
    user: User,
    db: AsyncSession,
) -> None:
    """Raise 403 if user cannot manage the project."""
    if user.is_admin:
        return
    allowed = await check_permission(user, project.id, "manage_project", db)
    if not allowed:
        raise PermissionDeniedError("You do not have permission to manage this project")


async def _require_project_access(
    project,
    user: User,
    db: AsyncSession,
) -> None:
    """Raise 404 if user cannot access the project.

    Returns 404 (not 403) for private projects to prevent enumeration.
    """
    if user.is_admin:
        return
    if not project.is_public:
        member_result = await db.execute(
            select(Member).where(
                Member.project_id == project.id,
                Member.user_id == user.id,
            )
        )
        if member_result.scalar_one_or_none() is None:
            raise NotFoundError(f"Project '{project.key}' not found")


def _project_out(project, parent_key: str | None) -> ProjectOut:
    return ProjectOut(
        id=project.id,
        name=project.name,
        identifier=project.identifier,
        key=project.key,
        description=project.description,
        parent_id=project.parent_id,
        parent_key=parent_key,
        path=project.path,
        is_public=project.is_public,
        inherit_members=project.inherit_members,
        status=project.status,
        issue_sequence=project.issue_sequence,
        created_at=project.created_at,
        updated_at=project.updated_at,
    )


# ---------------------------------------------------------------------------
# Project CRUD
# ---------------------------------------------------------------------------


@router.get("", response_model=PaginatedResponse[ProjectOut])
async def list_projects(
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PaginatedResponse[ProjectOut]:
    projects, total = await _service.list_projects(db, current_user, offset=offset, limit=limit)

    items = []
    for p in projects:
        parent_key = await _service.get_parent_key(db, p)
        items.append(_project_out(p, parent_key))

    return PaginatedResponse(total_count=total, offset=offset, limit=limit, items=items)


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
async def create_project(
    data: ProjectCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectOut:
    if not current_user.is_admin:
        raise PermissionDeniedError("Only admins can create projects")

    project = await _service.create(db, data, current_user)
    parent_key = await _service.get_parent_key(db, project)
    return _project_out(project, parent_key)


@router.get("/{key}", response_model=ProjectOut)
async def get_project(
    key: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectOut:
    project = await _service.get_by_key(db, key.upper())
    await _require_project_access(project, current_user, db)

    parent_key = await _service.get_parent_key(db, project)
    return _project_out(project, parent_key)


@router.patch("/{key}", response_model=ProjectOut)
async def update_project(
    key: str,
    data: ProjectUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectOut:
    project = await _service.get_by_key(db, key.upper())
    await _require_manage(project, current_user, db)

    project = await _service.update(db, project, data)
    parent_key = await _service.get_parent_key(db, project)
    return _project_out(project, parent_key)


@router.delete("/{key}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    key: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    project = await _service.get_by_key(db, key.upper())
    await _require_manage(project, current_user, db)
    await _service.delete(db, project)


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------


@router.get("/{key}/members", response_model=list[MemberOut])
async def list_members(
    key: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[MemberOut]:
    project = await _service.get_by_key(db, key.upper())
    await _require_project_access(project, current_user, db)
    members = await _service.list_members(db, project)
    return [MemberOut(**m) for m in members]


@router.post("/{key}/members", response_model=MemberOut, status_code=status.HTTP_201_CREATED)
async def add_member(
    key: str,
    data: MemberAdd,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MemberOut:
    project = await _service.get_by_key(db, key.upper())
    await _require_manage(project, current_user, db)

    await _service.add_member(db, project, data.user_id, data.role_ids)

    # Return the member's current state
    members = await _service.list_members(db, project)
    for m in members:
        if m["user_id"] == data.user_id:
            return MemberOut(**m)

    raise AppError(code="internal_error", message="Member not found after add", status_code=500)


@router.delete("/{key}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    key: str,
    user_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    project = await _service.get_by_key(db, key.upper())
    await _require_manage(project, current_user, db)
    await _service.remove_member(db, project, user_id)


# ---------------------------------------------------------------------------
# Modules
# ---------------------------------------------------------------------------


@router.get("/{key}/modules", response_model=ModulesOut)
async def get_modules(
    key: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ModulesOut:
    project = await _service.get_by_key(db, key.upper())
    await _require_project_access(project, current_user, db)
    modules = await _service.get_modules(db, project)
    return ModulesOut(modules=modules)


@router.patch("/{key}/modules", response_model=ModulesOut)
async def update_modules(
    key: str,
    data: ModuleToggle,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ModulesOut:
    project = await _service.get_by_key(db, key.upper())
    await _require_manage(project, current_user, db)

    modules = await _service.set_modules(db, project, data.modules)
    return ModulesOut(modules=modules)
