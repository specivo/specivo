"""Metadata schema service — CRUD and JSON Schema validation for issue metadata.

Validation judges the change, not the state of the issue: a schema error
counts only when it touches a top-level key the write changes (every key, on
create). An issue holding an old invalid value stays editable, and tightening a
schema never traps existing data; new invalid input is refused.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any

import jsonschema
from jsonschema import Draft202012Validator
from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import AppError, NotFoundError, ValidationError
from specivo.core.i18n import gettext as _
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
            message=_("Invalid JSON Schema: {reason}").format(reason=exc.message),
            field="schema_definition",
            details={"schema_path": list(exc.absolute_schema_path)},
        )


def _as_json(value: object) -> str:
    """*value* in a form that tells JSON types apart: in Python ``True == 1``, in JSON they differ."""
    return json.dumps(value, sort_keys=True, default=str)


def changed_metadata_keys(metadata: dict, previous: dict | None) -> set[str]:
    """Return the top-level keys *metadata* sets, changes or removes compared to *previous*.

    ``previous=None`` is a create: every key of *metadata* counts as changed.
    """
    if previous is None:
        return set(metadata)
    return {
        key
        for key in metadata.keys() | previous.keys()
        if key not in metadata or key not in previous or _as_json(metadata[key]) != _as_json(previous[key])
    }


def _additional_keys(metadata: dict, schema_definition: dict) -> list[str]:
    """Keys of *metadata* that neither ``properties`` nor ``patternProperties`` name."""
    props = schema_definition.get("properties") or {}
    patterns = list((schema_definition.get("patternProperties") or {}).keys())
    return [key for key in metadata if key not in props and not any(re.search(p, key) for p in patterns)]


def _missing_required(error: jsonschema.exceptions.ValidationError, metadata: dict) -> list[str]:
    """Names a root ``required`` error lists that *metadata* does not have."""
    required = error.validator_value if isinstance(error.validator_value, list) else []
    return [str(name) for name in required if name not in metadata]


def _counted_error(
    error: jsonschema.exceptions.ValidationError,
    metadata: dict,
    schema_definition: dict,
    changed: set[str],
    is_create: bool,
) -> tuple[bool, str | None]:
    """Return whether *error* counts for this write, and the top-level key it is about.

    - An error under a key counts when that key changed.
    - A root ``required`` error counts on create, or when the missing key changed
      (it was removed by this write).
    - A root ``additionalProperties`` error counts when an unexpected key changed.
    - Any other root error counts when anything changed.
    """
    path = list(error.absolute_path)
    if path:
        key = str(path[0])
        return key in changed, key
    if error.validator == "required":
        missing = _missing_required(error, metadata)
        # One error per missing name; its message starts with the quoted name.
        named = next((name for name in missing if error.message.startswith(f"{name!r} ")), None)
        required_key = named if named is not None else (missing[0] if missing else None)
        return required_key is not None and (is_create or required_key in changed), required_key
    if error.validator == "additionalProperties":
        extra = [key for key in _additional_keys(metadata, schema_definition) if key in changed]
        return bool(extra), extra[0] if extra else None
    return bool(changed) or is_create, None


def _validator(schema_definition: dict) -> jsonschema.protocols.Validator:
    validator_cls = jsonschema.validators.validator_for(schema_definition, default=Draft202012Validator)
    return validator_cls(schema_definition)


def schema_error(
    metadata: dict,
    schema_definition: dict,
    schema_name: str,
    changed: set[str],
    *,
    is_create: bool,
) -> ValidationError | None:
    """Return the first error of *schema_definition* that touches *changed*, as a ``ValidationError``.

    The message names the key and the schema; the reason from jsonschema stays
    in English. ``None`` when nothing this write changed breaks the schema.
    """
    for error in _validator(schema_definition).iter_errors(metadata):
        counts, key = _counted_error(error, metadata, schema_definition, changed, is_create)
        if not counts:
            continue
        details = {"schema_name": schema_name, "json_path": list(error.absolute_path)}
        if key is None:
            # jsonschema's message for a root error can quote the whole object, values included.
            message = _("Metadata does not match schema '{schema}': {reason}").format(
                schema=schema_name, reason=f"it breaks the '{error.validator}' rule"
            )
            return ValidationError(message=message, field="metadata", details=details)
        message = _("Metadata field '{key}' does not match schema '{schema}': {reason}").format(
            key=key, schema=schema_name, reason=error.message
        )
        return ValidationError(message=message, field=f"metadata.{key}", details=details)
    return None


def invalid_keys(metadata: dict, schema_definition: dict) -> set[str]:
    """Return the top-level keys of *metadata* that break *schema_definition*, judged in full.

    A root error that names no key (a wrong type for the whole object, say) is
    reported as ``""``.
    """
    keys: set[str] = set()
    for error in _validator(schema_definition).iter_errors(metadata):
        path = list(error.absolute_path)
        if path:
            keys.add(str(path[0]))
        elif error.validator == "required":
            keys.update(_missing_required(error, metadata))
        elif error.validator == "additionalProperties":
            keys.update(_additional_keys(metadata, schema_definition))
        else:
            keys.add("")
    return keys


@dataclass
class InvalidIssues:
    """Issues of one project whose metadata breaks one schema, as ``metadata-check`` reports them.

    Holds issue keys and metadata key names only, never values.
    """

    project_key: str
    schema_name: str
    issue_keys: list[str] = dataclass_field(default_factory=list)
    # metadata key -> number of issues where it breaks the schema
    keys: dict[str, int] = dataclass_field(default_factory=dict)


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
        locked = await session.execute(select(MetadataSchema).where(MetadataSchema.id == schema.id).with_for_update())
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

    async def schemas_for(
        self, session: AsyncSession, project_id: int, tracker_id: int, content_type: str
    ) -> list[MetadataSchema]:
        """Schemas that apply to *project_id* + *tracker_id*: project-wide first, then tracker-specific."""
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
                MetadataSchema.tracker_id.asc().nullsfirst(),
                MetadataSchema.name.asc(),
            )
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def validate_metadata(
        self,
        session: AsyncSession,
        project_id: int,
        tracker_id: int,
        metadata: dict,
        content_type: str = "issue",
        *,
        previous: dict | None = None,
    ) -> None:
        """Validate the keys this write changes in *metadata* against all matching schemas.

        Matching schemas are those of *project_id* that are project-wide or
        belong to *tracker_id*. Without any, every value is accepted.

        *previous* is the metadata before the write; ``None`` means create, where
        every key counts as changed and a schema's ``required`` keys must be
        present. On an update, only errors touching a changed top-level key
        count (see ``_counted_error``): an old invalid value under another key
        does not block the write. When nothing changed, no schema is read.

        Raises ``ValidationError`` (422, ``field="metadata.<key>"``) on the
        first counted violation.
        """
        changed = changed_metadata_keys(metadata, previous)
        is_create = previous is None
        if not changed and not is_create:
            return
        for schema in await self.schemas_for(session, project_id, tracker_id, content_type):
            error = schema_error(metadata, schema.schema_definition, schema.name, changed, is_create=is_create)
            if error is not None:
                raise error

    async def find_invalid_issues(
        self,
        session: AsyncSession,
        project_id: int | None = None,
        *,
        batch_size: int = 500,
    ) -> list[InvalidIssues]:
        """Return, per project and schema, the issues whose stored metadata breaks it in full.

        Read-only. Issues are read in batches by id, so a large instance is not
        loaded at once. Only projects that have issue schemas are scanned.
        """
        from specivo.models.project import Project

        stmt = select(MetadataSchema).where(MetadataSchema.content_type == "issue")
        if project_id is not None:
            stmt = stmt.where(MetadataSchema.project_id == project_id)
        schemas = list((await session.execute(stmt)).scalars().all())
        if not schemas:
            return []
        by_project: dict[int, list[MetadataSchema]] = {}
        for schema in schemas:
            by_project.setdefault(schema.project_id, []).append(schema)

        reports: dict[tuple[int, int], InvalidIssues] = {}
        rows = await session.execute(select(Project.id, Project.key).where(Project.id.in_(by_project)))
        project_keys: dict[int, str] = {pid: key for pid, key in rows}
        for pid in sorted(by_project, key=lambda p: project_keys.get(p, "")):
            project_schemas = sorted(by_project[pid], key=lambda s: (s.tracker_id is not None, s.name))
            async for issue_id, key, tracker_id, metadata in self._issue_batches(session, pid, batch_size):
                for schema in project_schemas:
                    if schema.tracker_id is not None and schema.tracker_id != tracker_id:
                        continue
                    bad = invalid_keys(metadata or {}, schema.schema_definition)
                    if not bad:
                        continue
                    report = reports.setdefault(
                        (pid, schema.id), InvalidIssues(project_key=project_keys.get(pid, ""), schema_name=schema.name)
                    )
                    report.issue_keys.append(key)
                    for name in bad:
                        report.keys[name] = report.keys.get(name, 0) + 1
        return list(reports.values())

    @staticmethod
    async def _issue_batches(
        session: AsyncSession, project_id: int, batch_size: int
    ) -> AsyncIterator[tuple[int, str, int, dict[str, Any]]]:
        """Yield ``(id, display key, tracker_id, metadata)`` for a project's issues, *batch_size* at a time."""
        last_id = 0
        while True:
            rows: Iterable[Any] = (
                await session.execute(
                    select(Issue.id, Issue.project_key, Issue.sequence_number, Issue.tracker_id, Issue.issue_metadata)
                    .where(Issue.project_id == project_id, Issue.id > last_id)
                    .order_by(Issue.id)
                    .limit(batch_size)
                )
            ).all()
            rows = list(rows)
            if not rows:
                return
            for issue_id, project_key, seq, tracker_id, metadata in rows:
                yield issue_id, f"{project_key}-{seq}", tracker_id, metadata
            last_id = rows[-1][0]
