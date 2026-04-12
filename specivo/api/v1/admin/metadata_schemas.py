"""Admin metadata schemas API — CRUD for JSON Schema definitions on issue metadata."""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.api.v1.admin import require_admin_api
from specivo.core.database import get_db
from specivo.models.user import User
from specivo.schemas.metadata_schema import (
    MetadataSchemaCreate,
    MetadataSchemaOut,
    MetadataSchemaUpdate,
    MetadataSchemaUsageOut,
)
from specivo.services.metadata_schema_service import MetadataSchemaService
from specivo.services.project_service import ProjectService

router = APIRouter(tags=["admin"])
_project_service = ProjectService()
_schema_service = MetadataSchemaService()


@router.post(
    "/admin/projects/{project_key}/metadata-schemas/",
    response_model=MetadataSchemaOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_metadata_schema(
    project_key: str,
    data: MetadataSchemaCreate,
    current_user: User = Depends(require_admin_api),
    db: AsyncSession = Depends(get_db),
) -> MetadataSchemaOut:
    """Create a metadata schema for a project (admin only)."""
    project = await _project_service.get_by_key(db, project_key.upper())
    schema = await _schema_service.create(db, project.id, data)
    return MetadataSchemaOut.model_validate(schema)


@router.get(
    "/admin/projects/{project_key}/metadata-schemas/",
    response_model=list[MetadataSchemaOut],
)
async def list_metadata_schemas(
    project_key: str,
    current_user: User = Depends(require_admin_api),
    db: AsyncSession = Depends(get_db),
) -> list[MetadataSchemaOut]:
    """List all metadata schemas for a project (admin only)."""
    project = await _project_service.get_by_key(db, project_key.upper())
    schemas = await _schema_service.list_for_project(db, project.id)
    return [MetadataSchemaOut.model_validate(s) for s in schemas]


@router.get(
    "/admin/projects/{project_key}/metadata-schemas/{schema_id}/",
    response_model=MetadataSchemaOut,
)
async def get_metadata_schema(
    project_key: str,
    schema_id: int,
    current_user: User = Depends(require_admin_api),
    db: AsyncSession = Depends(get_db),
) -> MetadataSchemaOut:
    """Get a single metadata schema by ID (admin only)."""
    project = await _project_service.get_by_key(db, project_key.upper())
    schema = await _schema_service.get_by_id(db, schema_id, project.id)
    return MetadataSchemaOut.model_validate(schema)


@router.patch(
    "/admin/projects/{project_key}/metadata-schemas/{schema_id}/",
    response_model=MetadataSchemaOut,
)
async def update_metadata_schema(
    project_key: str,
    schema_id: int,
    data: MetadataSchemaUpdate,
    current_user: User = Depends(require_admin_api),
    db: AsyncSession = Depends(get_db),
) -> MetadataSchemaOut:
    """Update a metadata schema (admin only)."""
    project = await _project_service.get_by_key(db, project_key.upper())
    schema = await _schema_service.get_by_id(db, schema_id, project.id)
    updated = await _schema_service.update(db, schema, data)
    return MetadataSchemaOut.model_validate(updated)


@router.get(
    "/admin/projects/{project_key}/metadata-schemas/{schema_id}/usage/",
    response_model=MetadataSchemaUsageOut,
)
async def get_metadata_schema_usage(
    project_key: str,
    schema_id: int,
    current_user: User = Depends(require_admin_api),
    db: AsyncSession = Depends(get_db),
) -> MetadataSchemaUsageOut:
    """Return usage count for a metadata schema (admin only)."""
    project = await _project_service.get_by_key(db, project_key.upper())
    schema = await _schema_service.get_by_id(db, schema_id, project.id)
    count = await _schema_service.count_usages(db, schema)
    return MetadataSchemaUsageOut(schema_id=schema.id, name=schema.name, usage_count=count)


@router.delete(
    "/admin/projects/{project_key}/metadata-schemas/{schema_id}/",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_metadata_schema(
    project_key: str,
    schema_id: int,
    current_user: User = Depends(require_admin_api),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a metadata schema (admin only). Rejects if in use."""
    project = await _project_service.get_by_key(db, project_key.upper())
    schema = await _schema_service.get_by_id(db, schema_id, project.id)
    await _schema_service.delete_safe(db, schema)
