"""Repository integration endpoints: CRUD, secret rotation and the delivery log.

Every route needs ``manage_project``. A private project the caller cannot
reach answers 404, like every project route. The secret is returned only by
create and rotate-secret.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.constants import DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT
from specivo.core.database import DB_SESSION
from specivo.core.exceptions import PermissionDeniedError
from specivo.core.security import get_current_user
from specivo.models.project import Project
from specivo.models.user import User
from specivo.schemas.common import PaginatedResponse
from specivo.schemas.repository import (
    RepositoryDeliveryOut,
    RepositoryIntegrationCreate,
    RepositoryIntegrationOut,
    RepositoryIntegrationSecretOut,
    RepositoryIntegrationUpdate,
)
from specivo.services.permission_service import Permission, check_permission
from specivo.services.project_service import ProjectService
from specivo.services.repository_service import (
    RepositoryIntegrationService,
    integration_out,
    integration_secret_out,
)

router = APIRouter(prefix="/projects/{project_key}/repositories", tags=["repositories"])
_project_service = ProjectService()
_service = RepositoryIntegrationService()


async def _managed_project(project_key: str, user: User, db: AsyncSession) -> Project:
    """The project, if the caller may manage it: 404 when they cannot see it, 403 when they cannot manage it."""
    project = await _project_service.get_by_key(db, project_key)
    await _project_service.require_project_access(db, project, user)
    if not await check_permission(user, project.id, Permission.MANAGE_PROJECT, db):
        raise PermissionDeniedError("Permission required: manage_project")
    return project


@router.get("/", response_model=list[RepositoryIntegrationOut])
async def list_repositories(
    project_key: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = DB_SESSION,
) -> list[RepositoryIntegrationOut]:
    """List the repositories connected to the project."""
    project = await _managed_project(project_key, current_user, db)
    return [integration_out(i) for i in await _service.list_for_project(db, project.id)]


@router.post("/", response_model=RepositoryIntegrationSecretOut, status_code=status.HTTP_201_CREATED)
async def create_repository(
    project_key: str,
    data: RepositoryIntegrationCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = DB_SESSION,
) -> RepositoryIntegrationSecretOut:
    """Connect a repository.

    The response carries ``hook_url`` and the generated ``secret`` in clear;
    the secret is never shown again (use ``rotate-secret`` for a new one).
    """
    project = await _managed_project(project_key, current_user, db)
    integration, secret = await _service.create(db, project.id, data, current_user)
    return integration_secret_out(integration, secret)


@router.get("/{integration_id}/", response_model=RepositoryIntegrationOut)
async def get_repository(
    project_key: str,
    integration_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = DB_SESSION,
) -> RepositoryIntegrationOut:
    """One connected repository."""
    project = await _managed_project(project_key, current_user, db)
    return integration_out(await _service.get(db, integration_id, project.id))


@router.patch("/{integration_id}/", response_model=RepositoryIntegrationOut)
async def update_repository(
    project_key: str,
    integration_id: int,
    data: RepositoryIntegrationUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = DB_SESSION,
) -> RepositoryIntegrationOut:
    """Change the repository name or URL, the scope, the settings, or enable/disable it."""
    project = await _managed_project(project_key, current_user, db)
    integration = await _service.update(db, integration_id, project.id, data.model_dump(exclude_unset=True))
    return integration_out(integration)


@router.delete("/{integration_id}/", status_code=status.HTTP_204_NO_CONTENT)
async def delete_repository(
    project_key: str,
    integration_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = DB_SESSION,
) -> None:
    """Disconnect a repository. Its deliveries and commit records go; issue journals stay."""
    project = await _managed_project(project_key, current_user, db)
    await _service.delete(db, integration_id, project.id)


@router.post("/{integration_id}/rotate-secret/", response_model=RepositoryIntegrationSecretOut)
async def rotate_repository_secret(
    project_key: str,
    integration_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = DB_SESSION,
) -> RepositoryIntegrationSecretOut:
    """Generate a new secret, returned once. The old secret stops working at once."""
    project = await _managed_project(project_key, current_user, db)
    integration, secret = await _service.rotate_secret(db, integration_id, project.id)
    return integration_secret_out(integration, secret)


@router.get("/{integration_id}/deliveries/", response_model=PaginatedResponse[RepositoryDeliveryOut])
async def list_repository_deliveries(
    project_key: str,
    integration_id: int,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = DB_SESSION,
) -> PaginatedResponse[RepositoryDeliveryOut]:
    """The deliveries received for the repository, newest first, with their outcome."""
    project = await _managed_project(project_key, current_user, db)
    integration = await _service.get(db, integration_id, project.id)
    rows, total = await _service.list_deliveries(db, integration, offset=offset, limit=limit)
    return PaginatedResponse[RepositoryDeliveryOut](
        total_count=total,
        offset=offset,
        limit=limit,
        items=[RepositoryDeliveryOut.model_validate(r) for r in rows],
    )
