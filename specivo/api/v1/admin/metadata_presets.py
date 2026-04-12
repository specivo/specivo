"""Admin metadata presets API — list, enable, disable presets on projects."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.api.v1.admin import require_admin_api
from specivo.core.database import get_db
from specivo.core.exceptions import PermissionDeniedError
from specivo.core.security import get_current_user
from specivo.models.user import User
from specivo.schemas.metadata_schema import (
    MetadataPresetCreate,
    MetadataPresetOut,
    MetadataPresetUpdate,
    MetadataSchemaOut,
    PresetEnableRequest,
)
from specivo.services.metadata_preset_service import MetadataPresetService
from specivo.services.permission_service import check_permission
from specivo.services.project_service import ProjectService

router = APIRouter(tags=["admin-metadata"])
_preset_service = MetadataPresetService()
_project_service = ProjectService()


async def _require_project_manage(
    project_key: str,
    user: User,
    db: AsyncSession,
) -> int:
    """Resolve project and check manage_project permission. Returns project_id."""
    project = await _project_service.get_by_key(db, project_key.upper())
    if not user.is_admin:
        allowed = await check_permission(user, project.id, "manage_project", db)
        if not allowed:
            raise PermissionDeniedError("manage_project permission required")
    return project.id


# ---------------------------------------------------------------------------
# Global preset listing (admin only — see all available presets)
# ---------------------------------------------------------------------------


@router.get(
    "/admin/metadata-presets/",
    response_model=list[MetadataPresetOut],
    summary="List all available metadata presets",
)
async def list_presets(
    current_user: User = Depends(require_admin_api),
    db: AsyncSession = Depends(get_db),
) -> list[MetadataPresetOut]:
    """Return all metadata presets (built-in + custom)."""
    presets = await _preset_service.list_presets(db)
    return [MetadataPresetOut.model_validate(p) for p in presets]


@router.get(
    "/admin/metadata-presets/{slug}/",
    response_model=MetadataPresetOut,
    summary="Get a single metadata preset by slug",
)
async def get_preset(
    slug: str,
    current_user: User = Depends(require_admin_api),
    db: AsyncSession = Depends(get_db),
) -> MetadataPresetOut:
    """Return a single preset by slug."""
    preset = await _preset_service.get_preset(db, slug)
    return MetadataPresetOut.model_validate(preset)


@router.post(
    "/admin/metadata-presets/",
    response_model=MetadataPresetOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a custom metadata preset",
)
async def create_preset(
    data: MetadataPresetCreate,
    current_user: User = Depends(require_admin_api),
    db: AsyncSession = Depends(get_db),
) -> MetadataPresetOut:
    """Create a new custom metadata preset (admin only)."""
    preset = await _preset_service.create_custom(
        db,
        slug=data.slug,
        name=data.name,
        description=data.description,
        icon=data.icon,
        schema_definition=data.schema_definition,
    )
    return MetadataPresetOut.model_validate(preset)


@router.patch(
    "/admin/metadata-presets/{slug}/",
    response_model=MetadataPresetOut,
    summary="Update a metadata preset",
)
async def update_preset(
    slug: str,
    data: MetadataPresetUpdate,
    current_user: User = Depends(require_admin_api),
    db: AsyncSession = Depends(get_db),
) -> MetadataPresetOut:
    """Update a metadata preset (admin only)."""
    preset = await _preset_service.get_preset(db, slug)
    kwargs = data.model_dump(exclude_unset=True)
    updated = await _preset_service.update_preset(db, preset, **kwargs)
    return MetadataPresetOut.model_validate(updated)


@router.delete(
    "/admin/metadata-presets/{slug}/",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a custom metadata preset",
)
async def delete_preset(
    slug: str,
    current_user: User = Depends(require_admin_api),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Delete a custom metadata preset (admin only). Builtin presets return 403."""
    preset = await _preset_service.get_preset(db, slug)
    await _preset_service.delete_preset(db, preset)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Project-level preset management (manage_project permission)
# ---------------------------------------------------------------------------


@router.get(
    "/admin/projects/{project_key}/metadata-presets/",
    response_model=list[str],
    summary="List enabled preset slugs for a project",
)
async def list_enabled_presets(
    project_key: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[str]:
    """Return slugs of presets enabled on this project."""
    project_id = await _require_project_manage(project_key, current_user, db)
    return await _preset_service.list_enabled(db, project_id)


@router.post(
    "/admin/projects/{project_key}/metadata-presets/{slug}/enable/",
    response_model=MetadataSchemaOut,
    status_code=status.HTTP_201_CREATED,
    summary="Enable a metadata preset on a project",
)
async def enable_preset(
    project_key: str,
    slug: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    body: PresetEnableRequest | None = None,
) -> MetadataSchemaOut:
    """Enable a preset on a project, creating a MetadataSchema."""
    project_id = await _require_project_manage(project_key, current_user, db)
    tracker_id = body.tracker_id if body else None
    schema = await _preset_service.enable(db, project_id, slug, tracker_id)
    return MetadataSchemaOut.model_validate(schema)


@router.delete(
    "/admin/projects/{project_key}/metadata-presets/{slug}/disable/",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Disable a metadata preset on a project",
)
async def disable_preset(
    project_key: str,
    slug: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Disable a preset on a project, removing its MetadataSchema rows."""
    project_id = await _require_project_manage(project_key, current_user, db)
    await _preset_service.disable(db, project_id, slug)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
