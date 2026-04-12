"""Metadata schema discovery API — agents and users discover available fields."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import get_db
from specivo.core.security import get_current_user
from specivo.models.user import User
from specivo.schemas.metadata_schema import MetadataSchemaOut
from specivo.services.metadata_schema_service import MetadataSchemaService
from specivo.services.project_service import ProjectService

router = APIRouter(tags=["metadata"])
_schema_service = MetadataSchemaService()
_project_service = ProjectService()


@router.get(
    "/projects/{project_key}/metadata-schemas/",
    response_model=list[MetadataSchemaOut],
    summary="Discover metadata schemas for a project",
)
async def discover_schemas(
    project_key: str,
    tracker_id: int | None = Query(None, description="Filter by tracker"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[MetadataSchemaOut]:
    """Return metadata schemas applicable to a project.

    When ``tracker_id`` is provided, returns both project-wide schemas
    (tracker_id=NULL) and tracker-specific schemas. Agents use this to
    discover what metadata fields are available before creating issues.
    """
    project = await _project_service.get_by_key(db, project_key.upper())
    await _project_service.require_project_access(db, project, current_user)
    schemas = await _schema_service.list_for_project(db, project.id)
    if tracker_id is not None:
        schemas = [s for s in schemas if s.tracker_id is None or s.tracker_id == tracker_id]
    return [MetadataSchemaOut.model_validate(s) for s in schemas]
