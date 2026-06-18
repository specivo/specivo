"""Metadata preset service — list, enable, disable presets on projects."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import AppError, NotFoundError
from specivo.models.metadata_preset import MetadataPreset
from specivo.models.metadata_schema import MetadataSchema
from specivo.services.metadata_schema_service import MetadataSchemaService, validate_json_schema


class MetadataPresetService:
    """Manage metadata presets and their activation on projects."""

    async def list_presets(self, session: AsyncSession) -> list[MetadataPreset]:
        """Return all available presets, ordered by name."""
        stmt = select(MetadataPreset).order_by(MetadataPreset.name)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def get_preset(self, session: AsyncSession, slug: str) -> MetadataPreset:
        """Get a preset by slug or raise NotFoundError."""
        result = await session.execute(select(MetadataPreset).where(MetadataPreset.slug == slug))
        preset = result.scalar_one_or_none()
        if preset is None:
            raise NotFoundError(message=f"Metadata preset '{slug}' not found")
        return preset

    async def list_enabled(
        self,
        session: AsyncSession,
        project_id: int,
    ) -> list[str]:
        """Return slugs of presets enabled on a project."""
        stmt = select(MetadataSchema.preset_slug).where(
            MetadataSchema.project_id == project_id,
            MetadataSchema.preset_slug.isnot(None),
        )
        result = await session.execute(stmt)
        return [row[0] for row in result.all()]

    async def enable(
        self,
        session: AsyncSession,
        project_id: int,
        slug: str,
        tracker_id: int | None = None,
    ) -> MetadataSchema:
        """Enable a preset on a project by creating a MetadataSchema.

        If the preset is already enabled (same slug + project + tracker),
        raises a 409 Conflict.
        """
        preset = await self.get_preset(session, slug)

        # Check if already enabled for this project+tracker
        dup_stmt = select(MetadataSchema).where(
            MetadataSchema.project_id == project_id,
            MetadataSchema.preset_slug == slug,
        )
        if tracker_id is None:
            dup_stmt = dup_stmt.where(MetadataSchema.tracker_id.is_(None))
        else:
            dup_stmt = dup_stmt.where(MetadataSchema.tracker_id == tracker_id)

        existing = (await session.execute(dup_stmt)).scalar_one_or_none()
        if existing is not None:
            raise AppError(
                code="conflict",
                message=f"Preset '{slug}' is already enabled on this project",
                status_code=409,
            )

        schema = MetadataSchema(
            project_id=project_id,
            tracker_id=tracker_id,
            name=preset.name,
            description=preset.description,
            schema_definition=preset.schema_definition,
            preset_slug=slug,
        )
        session.add(schema)
        await session.flush()
        await session.refresh(schema)
        return schema

    async def disable(
        self,
        session: AsyncSession,
        project_id: int,
        slug: str,
    ) -> None:
        """Disable a preset on a project by deleting its MetadataSchema rows.

        Locks each schema row with ``SELECT ... FOR UPDATE`` before
        checking usage to prevent TOCTOU races. Raises 409 if any
        schema has issues with matching metadata keys.
        """
        stmt = (
            select(MetadataSchema)
            .where(
                MetadataSchema.project_id == project_id,
                MetadataSchema.preset_slug == slug,
            )
            .with_for_update()
        )
        result = await session.execute(stmt)
        schemas = list(result.scalars().all())
        if not schemas:
            raise NotFoundError(message=f"Preset '{slug}' is not enabled on this project")
        schema_svc = MetadataSchemaService()
        for schema in schemas:
            count = await schema_svc.count_usages(session, schema)
            if count > 0:
                raise AppError(
                    code="conflict",
                    message=f"Cannot disable: {count} issue(s) have metadata matching schema '{schema.name}'",
                    status_code=409,
                )
        for schema in schemas:
            await session.delete(schema)
        await session.flush()

    async def create_custom(
        self,
        session: AsyncSession,
        slug: str,
        name: str,
        description: str | None,
        icon: str,
        schema_definition: dict,
    ) -> MetadataPreset:
        """Create a custom (non-builtin) metadata preset."""
        validate_json_schema(schema_definition)

        # Case-insensitive uniqueness: also guards against colliding with a
        # built-in preset's slug.
        existing = await session.execute(
            select(MetadataPreset).where(func.lower(MetadataPreset.slug) == slug.lower())
        )
        if existing.scalar_one_or_none() is not None:
            raise AppError(
                code="conflict",
                message=f"Preset with slug '{slug}' already exists",
                status_code=409,
            )

        preset = MetadataPreset(
            slug=slug,
            name=name,
            description=description,
            icon=icon,
            is_builtin=False,
            schema_definition=schema_definition,
        )
        session.add(preset)
        await session.flush()
        await session.refresh(preset)
        return preset

    async def update_preset(
        self,
        session: AsyncSession,
        preset: MetadataPreset,
        **kwargs: object,
    ) -> MetadataPreset:
        """Update a preset with the provided fields."""
        if "slug" in kwargs and kwargs["slug"] is not None and preset.is_builtin:
            raise AppError(
                code="permission_denied",
                message="Cannot change slug of a builtin preset",
                status_code=403,
            )
        new_slug = kwargs.get("slug")
        if new_slug is not None and str(new_slug).lower() != preset.slug.lower():
            duplicate = await session.execute(
                select(MetadataPreset).where(
                    func.lower(MetadataPreset.slug) == str(new_slug).lower(),
                    MetadataPreset.id != preset.id,
                )
            )
            if duplicate.scalar_one_or_none() is not None:
                raise AppError(
                    code="conflict",
                    message=f"Preset with slug '{new_slug}' already exists",
                    status_code=409,
                )
        if "schema_definition" in kwargs and kwargs["schema_definition"] is not None:
            validate_json_schema(kwargs["schema_definition"])  # type: ignore[arg-type]

        for field in ("slug", "name", "description", "icon", "schema_definition"):
            if field in kwargs and kwargs[field] is not None:
                setattr(preset, field, kwargs[field])

        session.add(preset)
        await session.flush()
        await session.refresh(preset)
        return preset

    async def delete_preset(
        self,
        session: AsyncSession,
        preset: MetadataPreset,
    ) -> None:
        """Delete a preset. Builtin presets cannot be deleted.

        Also rejects deletion when any project has this preset enabled
        (i.e. MetadataSchema rows reference the preset's slug).
        """
        if preset.is_builtin:
            raise AppError(
                code="permission_denied",
                message="Cannot delete a builtin preset",
                status_code=403,
            )
        # Check for active schemas referencing this preset
        result = await session.execute(
            select(func.count())
            .select_from(MetadataSchema)
            .where(MetadataSchema.preset_slug == preset.slug)
        )
        active_count = result.scalar_one()
        if active_count > 0:
            raise AppError(
                code="conflict",
                message=f"Cannot delete: {active_count} project(s) have this preset enabled",
                status_code=409,
            )
        await session.delete(preset)
        await session.flush()
