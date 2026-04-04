"""Saved Filters API — CRUD for named filter presets per user/project."""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import get_db
from specivo.core.security import get_current_user
from specivo.models.user import User
from specivo.schemas.saved_filter import SavedFilterCreate, SavedFilterOut, SavedFilterUpdate
from specivo.services.project_service import ProjectService
from specivo.services.saved_filter_service import SavedFilterService

router = APIRouter(tags=["saved-filters"])
_service = SavedFilterService()
_project_service = ProjectService()


@router.post(
    "/projects/{project_key}/saved-filters/",
    response_model=SavedFilterOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_saved_filter(
    project_key: str,
    data: SavedFilterCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SavedFilterOut:
    """Create a new saved filter in the given project."""
    project = await _project_service.get_by_key(db, project_key.upper())
    sf = await _service.create(db, current_user, project.id, data)
    return SavedFilterOut.model_validate(sf)


@router.get(
    "/projects/{project_key}/saved-filters/",
    response_model=list[SavedFilterOut],
)
async def list_saved_filters(
    project_key: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[SavedFilterOut]:
    """List saved filters for a project (own private + all public)."""
    project = await _project_service.get_by_key(db, project_key.upper())
    filters = await _service.list_for_project(db, current_user, project.id)
    return [SavedFilterOut.model_validate(f) for f in filters]


@router.get(
    "/projects/{project_key}/saved-filters/{filter_id}/",
    response_model=SavedFilterOut,
)
async def get_saved_filter(
    project_key: str,
    filter_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SavedFilterOut:
    """Get a single saved filter by ID."""
    await _project_service.get_by_key(db, project_key.upper())
    sf = await _service.get_by_id(db, filter_id)
    return SavedFilterOut.model_validate(sf)


@router.patch(
    "/projects/{project_key}/saved-filters/{filter_id}/",
    response_model=SavedFilterOut,
)
async def update_saved_filter(
    project_key: str,
    filter_id: int,
    data: SavedFilterUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SavedFilterOut:
    """Update a saved filter. Owner or admin only."""
    await _project_service.get_by_key(db, project_key.upper())
    sf = await _service.get_by_id(db, filter_id)
    sf = await _service.update(db, sf, data, current_user)
    return SavedFilterOut.model_validate(sf)


@router.delete(
    "/projects/{project_key}/saved-filters/{filter_id}/",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_saved_filter(
    project_key: str,
    filter_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a saved filter. Owner or admin only."""
    await _project_service.get_by_key(db, project_key.upper())
    sf = await _service.get_by_id(db, filter_id)
    await _service.delete(db, sf, current_user)
