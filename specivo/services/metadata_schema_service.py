"""Metadata schema service — CRUD and JSON Schema validation for issue metadata."""

from __future__ import annotations

import logging

import jsonschema
from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import AppError, NotFoundError, ValidationError
from specivo.core.features import get_feature_registry
from specivo.models.issue import Issue
from specivo.models.metadata_schema import MetadataSchema
from specivo.schemas.metadata_schema import MetadataSchemaCreate, MetadataSchemaUpdate

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public helpers (also used by unit tests directly)
# ---------------------------------------------------------------------------


def validate_json_schema(schema_definition: dict) -> None:
    """Validate that *schema_definition* is itself a valid JSON Schema.

    Raises ``ValidationError`` if the definition uses invalid JSON Schema
    constructs (e.g. ``{"type": "not_a_real_type"}``).
    """
    # jsonschema.validators.validator_for returns the appropriate validator
    # class for the schema draft.  Calling check_schema() validates the
    # schema definition itself (not data against a schema).
    validator_cls = jsonschema.validators.validator_for(schema_definition)
    try:
        validator_cls.check_schema(schema_definition)
    except jsonschema.exceptions.SchemaError as exc:
        raise ValidationError(
            message=f"Invalid JSON Schema: {exc.message}",
            field="schema_definition",
            details={"schema_path": list(exc.absolute_schema_path)},
        )


def validate_metadata_against_schema(
    metadata: dict,
    schema_definition: dict,
    schema_name: str,
) -> None:
    """Validate *metadata* against a single JSON Schema definition.

    Raises ``ValidationError`` with a human-readable message referencing
    *schema_name* when validation fails.
    """
    try:
        jsonschema.validate(instance=metadata, schema=schema_definition)
    except jsonschema.exceptions.ValidationError as exc:
        raise ValidationError(
            message=f"Metadata validation failed for schema '{schema_name}': {exc.message}",
            field="metadata",
            details={"schema_name": schema_name, "json_path": list(exc.absolute_path)},
        )


# ---------------------------------------------------------------------------
# Service class
# ---------------------------------------------------------------------------


class MetadataSchemaService:
    """Service layer for MetadataSchema operations."""

    # -----------------------------------------------------------------------
    # CRUD
    # -----------------------------------------------------------------------

    async def create(
        self,
        session: AsyncSession,
        project_id: int,
        data: MetadataSchemaCreate,
    ) -> MetadataSchema:
        """Create a new metadata schema for a project."""
        validate_json_schema(data.schema_definition)

        # Manual uniqueness check — PostgreSQL unique constraints treat
        # NULL != NULL, so (project_id, NULL, name) won't be caught by the DB.
        dup_stmt = select(MetadataSchema).where(
            MetadataSchema.project_id == project_id,
            MetadataSchema.name == data.name,
            MetadataSchema.content_type == data.content_type,
        )
        if data.tracker_id is None:
            dup_stmt = dup_stmt.where(MetadataSchema.tracker_id.is_(None))
        else:
            dup_stmt = dup_stmt.where(MetadataSchema.tracker_id == data.tracker_id)
        dup_result = await session.execute(dup_stmt)
        if dup_result.scalar_one_or_none() is not None:
            raise AppError(
                code="conflict",
                message=f"A metadata schema named '{data.name}' already exists for this project/tracker combination.",
                status_code=409,
            )

        schema = MetadataSchema(
            project_id=project_id,
            tracker_id=data.tracker_id,
            content_type=data.content_type,
            name=data.name,
            description=data.description,
            schema_definition=data.schema_definition,
        )
        session.add(schema)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            raise AppError(
                code="conflict",
                message=f"A metadata schema named '{data.name}' already exists for this project/tracker combination.",
                status_code=409,
            )
        logger.info(
            "Created metadata schema %d (%s) for project_id=%d tracker_id=%s",
            schema.id,
            schema.name,
            project_id,
            data.tracker_id,
        )
        return schema

    async def get_by_id(
        self,
        session: AsyncSession,
        schema_id: int,
        project_id: int,
    ) -> MetadataSchema:
        """Return a MetadataSchema by PK, scoped to a project.

        Raises ``NotFoundError`` if not found or belongs to a different project.
        """
        result = await session.execute(
            select(MetadataSchema).where(
                MetadataSchema.id == schema_id,
                MetadataSchema.project_id == project_id,
            )
        )
        schema = result.scalar_one_or_none()
        if schema is None:
            raise NotFoundError(f"Metadata schema {schema_id} not found")
        return schema

    async def list_for_project(
        self,
        session: AsyncSession,
        project_id: int,
        content_type: str | None = None,
    ) -> list[MetadataSchema]:
        """List all metadata schemas for a project, ordered by name.

        When *content_type* is provided, only schemas matching that
        content type are returned.  ``None`` returns schemas of all
        content types (legacy behaviour).
        """
        stmt = select(MetadataSchema).where(MetadataSchema.project_id == project_id)
        if content_type is not None:
            stmt = stmt.where(MetadataSchema.content_type == content_type)
        stmt = stmt.order_by(MetadataSchema.name.asc())
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def update(
        self,
        session: AsyncSession,
        schema: MetadataSchema,
        data: MetadataSchemaUpdate,
    ) -> MetadataSchema:
        """Apply a partial update to a metadata schema.

        Uses ``model_dump(exclude_unset=True)`` so that fields the client
        did not send are left untouched, while explicitly sent ``None``
        values (e.g. ``tracker_id: null``) are applied correctly.
        """
        updates = data.model_dump(exclude_unset=True)
        if "schema_definition" in updates and updates["schema_definition"] is not None:
            validate_json_schema(updates["schema_definition"])
        for field, value in updates.items():
            setattr(schema, field, value)
        session.add(schema)
        await session.flush()
        await session.refresh(schema)
        return schema

    async def delete(
        self,
        session: AsyncSession,
        schema: MetadataSchema,
    ) -> None:
        """Delete a metadata schema."""
        await session.delete(schema)
        await session.flush()

    async def count_usages(
        self,
        session: AsyncSession,
        schema: MetadataSchema,
    ) -> int:
        """Count issues whose issue_metadata contains any key from the schema's properties."""
        props = schema.schema_definition.get("properties", {})
        if not props:
            return 0
        keys = list(props.keys())
        stmt = (
            select(func.count())
            .select_from(Issue)
            .where(
                Issue.project_id == schema.project_id,
                text("issue_metadata ?| :keys"),
            )
        )
        result = await session.execute(stmt, {"keys": keys})
        return result.scalar_one()

    async def delete_safe(
        self,
        session: AsyncSession,
        schema: MetadataSchema,
    ) -> None:
        """Delete a schema only if no issues use its metadata keys.

        Locks the schema row with ``SELECT ... FOR UPDATE`` to prevent a
        concurrent request from creating matching metadata between the
        usage check and the delete.
        """
        locked = await session.execute(
            select(MetadataSchema)
            .where(MetadataSchema.id == schema.id)
            .with_for_update()
        )
        locked_schema = locked.scalar_one_or_none()
        if locked_schema is None:
            raise NotFoundError(f"Metadata schema {schema.id} not found")
        count = await self.count_usages(session, locked_schema)
        if count > 0:
            raise AppError(
                code="conflict",
                message=f"Cannot delete: {count} issue(s) have metadata matching this schema",
                status_code=409,
            )
        await self.delete(session, locked_schema)

    # -----------------------------------------------------------------------
    # Validation for issue metadata
    # -----------------------------------------------------------------------

    async def validate_metadata(
        self,
        session: AsyncSession,
        project_id: int,
        tracker_id: int,
        metadata: dict,
        content_type: str = "issue",
    ) -> None:
        """Validate *metadata* against all matching schemas.

        Finds schemas that match:
        - project_id + tracker_id  (tracker-specific)
        - project_id + tracker_id IS NULL  (project-wide)

        When no schemas exist for this combo, validation is a no-op
        (backward compatible — any metadata is accepted).

        Without the ``metadata_schema_validation`` feature (Enterprise),
        validation is skipped entirely — metadata is stored as-is.

        Raises ``ValidationError`` on the first schema violation.
        """
        registry = get_feature_registry()
        if not registry.has_feature("metadata_schema_validation"):
            return
        stmt = (
            select(MetadataSchema)
            .where(
                MetadataSchema.project_id == project_id,
                MetadataSchema.content_type == content_type,
                or_(
                    MetadataSchema.tracker_id == tracker_id,
                    MetadataSchema.tracker_id.is_(None),
                ),
            )
            .order_by(
                # Validate project-wide first, then tracker-specific
                MetadataSchema.tracker_id.asc().nullsfirst(),
                MetadataSchema.name.asc(),
            )
        )
        result = await session.execute(stmt)
        schemas = result.scalars().all()

        for schema in schemas:
            validate_metadata_against_schema(metadata, schema.schema_definition, schema.name)
