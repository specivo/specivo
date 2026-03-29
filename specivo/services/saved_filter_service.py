"""SavedFilterService — CRUD for named filter presets."""

from __future__ import annotations

import logging

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import NotFoundError, PermissionDeniedError
from specivo.models.saved_filter import SavedFilter
from specivo.models.user import User
from specivo.schemas.saved_filter import SavedFilterCreate, SavedFilterUpdate

logger = logging.getLogger(__name__)


class SavedFilterService:
    """Service layer for saved filter CRUD operations."""

    async def create(
        self,
        session: AsyncSession,
        user: User,
        project_id: int,
        data: SavedFilterCreate,
    ) -> SavedFilter:
        """Create a new saved filter for the given user and project."""
        saved_filter = SavedFilter(
            name=data.name,
            user_id=user.id,
            project_id=project_id,
            filter_definition=data.filter_definition,
            is_public=data.is_public,
        )
        session.add(saved_filter)
        await session.flush()
        logger.debug("Created saved filter %d for user %d", saved_filter.id, user.id)
        return saved_filter

    async def list_for_project(
        self,
        session: AsyncSession,
        user: User,
        project_id: int,
    ) -> list[SavedFilter]:
        """Return the user's private filters + all public filters for a project."""
        result = await session.execute(
            select(SavedFilter)
            .where(
                SavedFilter.project_id == project_id,
                or_(
                    SavedFilter.user_id == user.id,
                    SavedFilter.is_public.is_(True),
                ),
            )
            .order_by(SavedFilter.position, SavedFilter.name)
        )
        return list(result.scalars().all())

    async def get_by_id(
        self,
        session: AsyncSession,
        filter_id: int,
    ) -> SavedFilter:
        """Return a saved filter by ID or raise NotFoundError."""
        result = await session.execute(select(SavedFilter).where(SavedFilter.id == filter_id))
        saved_filter = result.scalar_one_or_none()
        if saved_filter is None:
            raise NotFoundError(f"Saved filter {filter_id} not found")
        return saved_filter

    async def update(
        self,
        session: AsyncSession,
        saved_filter: SavedFilter,
        data: SavedFilterUpdate,
        user: User,
    ) -> SavedFilter:
        """Update a saved filter. Only owner or admin may update."""
        if saved_filter.user_id != user.id and not user.is_admin:
            raise PermissionDeniedError("Only the filter owner or an admin can edit this filter")

        if data.name is not None:
            saved_filter.name = data.name
        if data.filter_definition is not None:
            saved_filter.filter_definition = data.filter_definition
        if data.is_public is not None:
            saved_filter.is_public = data.is_public

        await session.flush()
        await session.refresh(saved_filter)
        logger.debug("Updated saved filter %d", saved_filter.id)
        return saved_filter

    async def delete(
        self,
        session: AsyncSession,
        saved_filter: SavedFilter,
        user: User,
    ) -> None:
        """Delete a saved filter. Only owner or admin may delete."""
        if saved_filter.user_id != user.id and not user.is_admin:
            raise PermissionDeniedError("Only the filter owner or an admin can delete this filter")

        await session.delete(saved_filter)
        await session.flush()
        logger.debug("Deleted saved filter %d", saved_filter.id)
