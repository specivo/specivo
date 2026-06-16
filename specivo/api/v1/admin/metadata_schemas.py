"""Admin metadata schemas API — CRUD for JSON Schema definitions on issue metadata.

Endpoints are scoped to a project. Access requires either global admin
or the project-scoped ``manage_project`` permission, matching the pattern
used by metadata-presets and other project-settings endpoints.

Mutating operations (create / update / delete) emit security audit events.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import get_db
from specivo.core.exceptions import PermissionDeniedError
from specivo.core.security import get_current_user
from specivo.models.user import User
from specivo.schemas.metadata_schema import (
    MetadataSchemaCreate,
    MetadataSchemaOut,
    MetadataSchemaUpdate,
    MetadataSchemaUsageOut,
)
from specivo.services.metadata_schema_service import MetadataSchemaService
from specivo.services.permission_service import check_permission
from specivo.services.project_service import ProjectService
from specivo.services.security_audit_service import AuditEvent, SecurityAuditService

router = APIRouter(tags=["admin"])
_project_service = ProjectService()
_schema_service = MetadataSchemaService()
_audit = SecurityAuditService()


async def _require_project_manage(
    project_key: str,
    user: User,
    db: AsyncSession,
):
    """Resolve project and require manage_project permission (or global admin).

    Mirrors the helper in admin/metadata_presets.py so project managers can
    administer metadata schemas without needing the global is_admin flag.
    """
    project = await _project_service.get_by_key(db, project_key.upper())
    if not user.is_admin:
        allowed = await check_permission(user, project.id, "manage_project", db)
        if not allowed:
            raise PermissionDeniedError("manage_project permission required")
    return project


@router.post(
    "/admin/projects/{project_key}/metadata-schemas/",
    response_model=MetadataSchemaOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_metadata_schema(
    project_key: str,
    data: MetadataSchemaCreate,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MetadataSchemaOut:
    """Create a metadata schema for a project (manage_project permission)."""
    project = await _require_project_manage(project_key, current_user, db)
    schema = await _schema_service.create(db, project.id, data)
    try:
        await _audit.log_event(
            session=db,
            event_type=AuditEvent.METADATA_SCHEMA_CREATED,
            user_id=current_user.id,
            project_id=project.id,
            resource_type="metadata_schema",
            resource_id=schema.id,
            details={
                "project_key": project.key,
                "schema_id": schema.id,
                "name": schema.name,
                "tracker_id": schema.tracker_id,
                "preset_slug": schema.preset_slug,
            },
            request=request,
        )
    except Exception:
        pass
    return MetadataSchemaOut.model_validate(schema)


@router.get(
    "/admin/projects/{project_key}/metadata-schemas/",
    response_model=list[MetadataSchemaOut],
)
async def list_metadata_schemas(
    project_key: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[MetadataSchemaOut]:
    """List all metadata schemas for a project (manage_project permission)."""
    project = await _require_project_manage(project_key, current_user, db)
    schemas = await _schema_service.list_for_project(db, project.id)
    return [MetadataSchemaOut.model_validate(s) for s in schemas]


@router.get(
    "/admin/projects/{project_key}/metadata-schemas/{schema_id}/",
    response_model=MetadataSchemaOut,
)
async def get_metadata_schema(
    project_key: str,
    schema_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MetadataSchemaOut:
    """Get a single metadata schema by ID (manage_project permission)."""
    project = await _require_project_manage(project_key, current_user, db)
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
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MetadataSchemaOut:
    """Update a metadata schema (manage_project permission)."""
    project = await _require_project_manage(project_key, current_user, db)
    schema = await _schema_service.get_by_id(db, schema_id, project.id)
    changed_fields = sorted(data.model_dump(exclude_unset=True).keys())
    updated = await _schema_service.update(db, schema, data)
    try:
        await _audit.log_event(
            session=db,
            event_type=AuditEvent.METADATA_SCHEMA_UPDATED,
            user_id=current_user.id,
            project_id=project.id,
            resource_type="metadata_schema",
            resource_id=updated.id,
            details={
                "project_key": project.key,
                "schema_id": updated.id,
                "name": updated.name,
                "tracker_id": updated.tracker_id,
                "changed_fields": changed_fields,
            },
            request=request,
        )
    except Exception:
        pass
    return MetadataSchemaOut.model_validate(updated)


@router.get(
    "/admin/projects/{project_key}/metadata-schemas/{schema_id}/usage/",
    response_model=MetadataSchemaUsageOut,
)
async def get_metadata_schema_usage(
    project_key: str,
    schema_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MetadataSchemaUsageOut:
    """Return usage count for a metadata schema (manage_project permission)."""
    project = await _require_project_manage(project_key, current_user, db)
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
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a metadata schema (manage_project permission). Rejects if in use."""
    project = await _require_project_manage(project_key, current_user, db)
    schema = await _schema_service.get_by_id(db, schema_id, project.id)
    schema_name = schema.name
    schema_tracker_id = schema.tracker_id
    await _schema_service.delete_safe(db, schema)
    try:
        await _audit.log_event(
            session=db,
            event_type=AuditEvent.METADATA_SCHEMA_DELETED,
            user_id=current_user.id,
            project_id=project.id,
            resource_type="metadata_schema",
            resource_id=schema_id,
            details={
                "project_key": project.key,
                "schema_id": schema_id,
                "name": schema_name,
                "tracker_id": schema_tracker_id,
            },
            request=request,
        )
    except Exception:
        pass
