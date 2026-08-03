"""Projects API — CRUD, membership, and module management."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import get_db
from specivo.core.exceptions import AppError, PermissionDeniedError
from specivo.core.security import get_current_user
from specivo.models.user import User
from specivo.schemas.common import PaginatedResponse
from specivo.schemas.project import (
    MemberAdd,
    MemberOut,
    MemberUpdateRoles,
    ModulesOut,
    ModuleToggle,
    ProjectCreate,
    ProjectOut,
    ProjectUpdate,
)
from specivo.services.computed_metadata_service import computed_values
from specivo.services.permission_service import Permission, check_permission
from specivo.services.project_service import ProjectService
from specivo.services.security_audit_service import MemberAction, SecurityAuditService

router = APIRouter(prefix="/projects", tags=["projects"])
_service = ProjectService()
_audit = SecurityAuditService()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _require_manage(
    project,
    user: User,
    db: AsyncSession,
    request: Request | None = None,
) -> None:
    """Raise 403 if user cannot manage the project. Logs failed attempts."""
    if user.is_admin:
        return
    allowed = await check_permission(user, project.id, Permission.MANAGE_PROJECT, db)
    if not allowed:
        try:
            await _audit.log_member_change(
                session=db,
                action=MemberAction.PERMISSION_DENIED,
                user_id=user.id,
                project_id=project.id,
                target_user_id=0,
                target_login="",
                request=request,
            )
            await db.commit()
        except Exception:
            pass
        raise PermissionDeniedError("You do not have permission to manage this project")


async def _require_project_access(
    project,
    user: User,
    db: AsyncSession,
) -> None:
    """Raise 404 if user cannot access the project."""
    await _service.require_project_access(db, project, user)


async def _can_manage(project, user: User, db: AsyncSession) -> bool:
    """Non-raising variant of :func:`_require_manage`, for response shaping."""
    if user.is_admin:
        return True
    return await check_permission(user, project.id, Permission.MANAGE_PROJECT, db)


async def _disclosed_computed_metadata(project, user: User, db: AsyncSession) -> dict | None:
    """Return the project's computed metadata map, or ``None`` if undisclosed.

    Managers get the real map (``{}`` when unconfigured) so that a configured
    project is distinguishable from an unconfigured one; everyone else gets
    ``None``. See ``ProjectOut.computed_metadata`` for the three states.
    """
    if not await _can_manage(project, user, db):
        return None
    return computed_values(project.settings)


def _project_out(project, parent_key: str | None, computed_metadata: dict | None = None) -> ProjectOut:
    return ProjectOut(
        computed_metadata=computed_metadata,
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
        color=project.color,
        created_at=project.created_at,
        updated_at=project.updated_at,
    )


# ---------------------------------------------------------------------------
# Project CRUD
# ---------------------------------------------------------------------------


@router.get("/", response_model=PaginatedResponse[ProjectOut])
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


@router.post("/", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
async def create_project(
    data: ProjectCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectOut:
    if not current_user.is_admin:
        raise PermissionDeniedError("Only admins can create projects")

    project = await _service.create(db, data, current_user)
    await db.commit()  # commit before response to avoid reload race condition
    parent_key = await _service.get_parent_key(db, project)
    # Creation is admin-only, so the creator may always see the map.
    return _project_out(project, parent_key, computed_values(project.settings))


@router.get("/{key}/", response_model=ProjectOut)
async def get_project(
    key: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectOut:
    project = await _service.get_by_key(db, key.upper())
    await _require_project_access(project, current_user, db)

    parent_key = await _service.get_parent_key(db, project)
    computed = await _disclosed_computed_metadata(project, current_user, db)
    return _project_out(project, parent_key, computed)


@router.patch("/{key}/", response_model=ProjectOut)
async def update_project(
    key: str,
    data: ProjectUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectOut:
    project = await _service.get_by_key(db, key.upper())
    await _require_manage(project, current_user, db)

    project = await _service.update(db, project, data)
    await db.commit()  # commit before response to avoid reload race condition
    parent_key = await _service.get_parent_key(db, project)
    # _require_manage passed, so the caller may always see the map.
    return _project_out(project, parent_key, computed_values(project.settings))


@router.delete("/{key}/", status_code=status.HTTP_204_NO_CONTENT)
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


@router.get("/{key}/members/", response_model=list[MemberOut])
async def list_members(
    key: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[MemberOut]:
    project = await _service.get_by_key(db, key.upper())
    await _require_project_access(project, current_user, db)
    members = await _service.list_members(db, project)
    return [MemberOut(**m) for m in members]


@router.post("/{key}/members/", response_model=MemberOut, status_code=status.HTTP_201_CREATED)
async def add_member(
    key: str,
    data: MemberAdd,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MemberOut:
    project = await _service.get_by_key(db, key.upper())
    await _require_manage(project, current_user, db, request)

    await _service.add_member(db, project, data.user_id, data.role_ids)

    # Return the member's current state
    members = await _service.list_members(db, project)
    result: MemberOut | None = None
    for m in members:
        if m["user_id"] == data.user_id:
            result = MemberOut(**m)
            break

    if result is None:
        raise AppError(code="internal_error", message="Member not found after add", status_code=500)

    try:
        await _audit.log_member_change(
            session=db,
            action=MemberAction.ADDED,
            user_id=current_user.id,
            project_id=project.id,
            target_user_id=data.user_id,
            target_login=result.login,
            roles=result.roles,
            request=request,
        )
    except Exception:
        pass

    return result


@router.delete("/{key}/members/{user_id}/", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    key: str,
    user_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    project = await _service.get_by_key(db, key.upper())
    await _require_manage(project, current_user, db, request)

    # Capture member info before deletion for audit
    target_login = ""
    try:
        target = await db.execute(select(User.login).where(User.id == user_id))
        target_login = target.scalar_one_or_none() or ""
    except Exception:
        pass

    await _service.remove_member(db, project, user_id)

    try:
        await _audit.log_member_change(
            session=db,
            action=MemberAction.REMOVED,
            user_id=current_user.id,
            project_id=project.id,
            target_user_id=user_id,
            target_login=target_login,
            request=request,
        )
    except Exception:
        pass


@router.patch("/{key}/members/{user_id}/", response_model=MemberOut)
async def update_member_roles(
    key: str,
    user_id: int,
    data: MemberUpdateRoles,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MemberOut:
    """Replace all roles for a project member."""
    project = await _service.get_by_key(db, key.upper())
    await _require_manage(project, current_user, db, request)

    await _service.update_member_roles(db, project, user_id, data.role_ids)

    members = await _service.list_members(db, project)
    result: MemberOut | None = None
    for m in members:
        if m["user_id"] == user_id:
            result = MemberOut(**m)
            break

    if result is None:
        raise AppError(code="internal_error", message="Member not found after update", status_code=500)

    try:
        await _audit.log_member_change(
            session=db,
            action=MemberAction.ROLES_CHANGED,
            user_id=current_user.id,
            project_id=project.id,
            target_user_id=user_id,
            target_login=result.login,
            roles=result.roles,
            request=request,
        )
    except Exception:
        pass

    return result


# ---------------------------------------------------------------------------
# Modules
# ---------------------------------------------------------------------------


@router.get("/{key}/modules/", response_model=ModulesOut)
async def get_modules(
    key: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ModulesOut:
    project = await _service.get_by_key(db, key.upper())
    await _require_project_access(project, current_user, db)
    modules = await _service.get_modules(db, project)
    return ModulesOut(modules=modules)


@router.patch("/{key}/modules/", response_model=ModulesOut)
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
