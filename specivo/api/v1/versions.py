"""Versions API — CRUD and roadmap endpoints scoped to a project."""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import get_db
from specivo.core.exceptions import NotFoundError, PermissionDeniedError
from specivo.core.security import get_current_user
from specivo.models.project import Project
from specivo.models.user import User
from specivo.schemas.version import RoadmapEntry, VersionCreate, VersionOut, VersionUpdate
from specivo.services.permission_service import check_permission
from specivo.services.project_service import ProjectService
from specivo.services.version_service import VersionService

router = APIRouter(tags=["versions"])
_project_service = ProjectService()
_version_service = VersionService()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _get_project(project_key: str, db: AsyncSession) -> Project:
    """Resolve a project by key; raises NotFoundError if missing."""
    return await _project_service.get_by_key(db, project_key.upper())


async def _require_manage_versions(
    project: Project,
    user: User,
    db: AsyncSession,
) -> None:
    """Raise 403 if user lacks manage_versions on *project*."""
    if user.is_admin:
        return
    allowed = await check_permission(user, project.id, "manage_versions", db)
    if not allowed:
        raise PermissionDeniedError("You do not have permission to manage versions")


def _version_out(version, project_key: str) -> VersionOut:
    return VersionOut(
        id=version.id,
        name=version.name,
        description=version.description,
        status=version.status,
        effective_date=version.effective_date,
        sharing=version.sharing,
        wiki_page_title=version.wiki_page_title,
        project_key=project_key,
        created_at=version.created_at,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/projects/{project_key}/versions",
    response_model=list[VersionOut],
)
async def list_versions(
    project_key: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[VersionOut]:
    project = await _get_project(project_key, db)
    versions = await _version_service.list_for_project(db, project.id)
    return [_version_out(v, project.key) for v in versions]


@router.post(
    "/projects/{project_key}/versions",
    response_model=VersionOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_version(
    project_key: str,
    data: VersionCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> VersionOut:
    project = await _get_project(project_key, db)
    await _require_manage_versions(project, current_user, db)
    version = await _version_service.create(db, project, data)
    return _version_out(version, project.key)


@router.get(
    "/projects/{project_key}/versions/{version_id}",
    response_model=VersionOut,
)
async def get_version(
    project_key: str,
    version_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> VersionOut:
    project = await _get_project(project_key, db)
    version = await _version_service.get_by_id(db, version_id)
    if version.project_id != project.id:
        raise NotFoundError(f"Version {version_id} not found in project '{project.key}'")
    return _version_out(version, project.key)


@router.patch(
    "/projects/{project_key}/versions/{version_id}",
    response_model=VersionOut,
)
async def update_version(
    project_key: str,
    version_id: int,
    data: VersionUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> VersionOut:
    project = await _get_project(project_key, db)
    await _require_manage_versions(project, current_user, db)
    version = await _version_service.get_by_id(db, version_id)
    if version.project_id != project.id:
        raise NotFoundError(f"Version {version_id} not found in project '{project.key}'")
    version = await _version_service.update(db, version, data)
    return _version_out(version, project.key)


@router.delete(
    "/projects/{project_key}/versions/{version_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_version(
    project_key: str,
    version_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    project = await _get_project(project_key, db)
    await _require_manage_versions(project, current_user, db)
    version = await _version_service.get_by_id(db, version_id)
    if version.project_id != project.id:
        raise NotFoundError(f"Version {version_id} not found in project '{project.key}'")
    await _version_service.delete(db, version)


@router.get(
    "/projects/{project_key}/roadmap",
    response_model=list[RoadmapEntry],
)
async def get_roadmap(
    project_key: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[RoadmapEntry]:
    project = await _get_project(project_key, db)
    return await _version_service.roadmap(db, project)
