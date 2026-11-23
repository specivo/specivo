"""MCP tools: metadata schemas and per-entity metadata values."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import (
    ConflictError,
    NotFoundError,
    ValidationError,
)
from specivo.mcp.tools._common import _get_project, _log_tool, _require_permission
from specivo.models.user import User
from specivo.services.computed_metadata_service import load_project_settings, merge_computed
from specivo.services.security_audit_service import AuditEvent


async def _list_metadata_schemas(
    session: AsyncSession,
    user: User,
    project_key: str,
    tracker_id: int | None = None,
    content_type: str | None = None,
) -> str:
    """Discover metadata schemas for a project. Agents call this to learn
    what metadata fields are available before creating/updating issues."""
    from specivo.services.metadata_schema_service import MetadataSchemaService

    project = await _get_project(session, user, project_key)
    await _require_permission(session, user, project.id, "view_issues")

    svc = MetadataSchemaService()
    schemas = await svc.list_for_project(session, project.id, content_type=content_type)
    if tracker_id is not None:
        schemas = [s for s in schemas if s.tracker_id is None or s.tracker_id == tracker_id]

    if not schemas:
        return f"No metadata schemas configured for project {project.key}."

    lines = [f"Metadata schemas for {project.key}:", ""]
    for s in schemas:
        scope = f"tracker_id={s.tracker_id}" if s.tracker_id else "all trackers"
        preset = f"  (preset: {s.preset_slug})" if s.preset_slug else ""
        lines.append(f"  id={s.id}  [{s.content_type}] {s.name} ({scope}){preset}")
        props = s.schema_definition.get("properties", {})
        for field_name, field_def in props.items():
            ftype = field_def.get("type", "any")
            desc = field_def.get("description", "")
            enum = field_def.get("enum")
            extra = f"  values: {enum}" if enum else ""
            label = field_def.get("title")
            title = f' "{label}"' if isinstance(label, str) and label else ""
            lines.append(f"    {field_name}{title}: {ftype}  {desc}{extra}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Metadata schema management (create/update/delete)
# ---------------------------------------------------------------------------


async def _create_metadata_schema(
    session: AsyncSession,
    user: User,
    project_key: str,
    name: str,
    schema: dict,
    content_type: str = "issue",
    tracker_id: int | None = None,
    description: str | None = None,
) -> str:
    """Create a metadata schema for a project.

    Requires the ``manage_project`` permission on the target project.
    The mutation is recorded in the security audit log
    (``METADATA_SCHEMA_CREATED``).

    *schema* is the full JSON Schema body (the ``schema_definition``
    field on the underlying model). Use specivo_list_metadata_schemas
    to inspect existing schemas first.
    """
    from specivo.schemas.metadata_schema import MetadataSchemaCreate
    from specivo.services.metadata_schema_service import MetadataSchemaService

    project = await _get_project(session, user, project_key)
    await _require_permission(session, user, project.id, "manage_project")

    payload = MetadataSchemaCreate(
        name=name,
        tracker_id=tracker_id,
        content_type=content_type,
        description=description,
        schema_definition=schema,
    )
    svc = MetadataSchemaService()
    created = await svc.create(session, project.id, payload)
    await _log_tool(
        session,
        user,
        AuditEvent.METADATA_SCHEMA_CREATED,
        "create_metadata_schema",
        {
            "project_key": project.key,
            "schema_id": created.id,
            "name": created.name,
            "tracker_id": created.tracker_id,
        },
        project_id=project.id,
    )
    return (
        f"Created metadata schema id={created.id} name='{created.name}' "
        f"content_type={created.content_type} tracker_id={created.tracker_id} "
        f"in project {project.key}."
    )


async def _update_metadata_schema(
    session: AsyncSession,
    user: User,
    project_key: str,
    schema_id: int,
    name: str | None = None,
    tracker_id: int | None = None,
    schema: dict | None = None,
    description: str | None = None,
) -> str:
    """Patch a metadata schema.

    Requires the ``manage_project`` permission on the target project.
    The mutation is recorded in the security audit log
    (``METADATA_SCHEMA_UPDATED``).

    Only provided fields are changed. To inspect the current
    definition before patching, call specivo_list_metadata_schemas.
    """
    from specivo.schemas.metadata_schema import MetadataSchemaUpdate
    from specivo.services.metadata_schema_service import MetadataSchemaService

    project = await _get_project(session, user, project_key)
    await _require_permission(session, user, project.id, "manage_project")

    svc = MetadataSchemaService()
    existing = await svc.get_by_id(session, schema_id, project.id)

    update_kwargs: dict[str, Any] = {}
    if name is not None:
        update_kwargs["name"] = name
    if tracker_id is not None:
        update_kwargs["tracker_id"] = tracker_id
    if schema is not None:
        update_kwargs["schema_definition"] = schema
    if description is not None:
        update_kwargs["description"] = description
    if not update_kwargs:
        raise ValidationError("no fields provided to update")

    payload = MetadataSchemaUpdate(**update_kwargs)
    updated = await svc.update(session, existing, payload)
    changed_fields = sorted(update_kwargs.keys())
    await _log_tool(
        session,
        user,
        AuditEvent.METADATA_SCHEMA_UPDATED,
        "update_metadata_schema",
        {
            "project_key": project.key,
            "schema_id": updated.id,
            "name": updated.name,
            "tracker_id": updated.tracker_id,
            "changed_fields": changed_fields,
        },
        project_id=project.id,
    )
    return (
        f"Updated metadata schema id={updated.id} name='{updated.name}' "
        f"in project {project.key}. Changed: {', '.join(changed_fields)}."
    )


async def _delete_metadata_schema(
    session: AsyncSession,
    user: User,
    project_key: str,
    schema_id: int,
) -> str:
    """Delete a metadata schema.

    Requires the ``manage_project`` permission on the target project.
    The mutation is recorded in the security audit log
    (``METADATA_SCHEMA_DELETED``).

    Fails with a conflict error if any issue still has metadata
    matching the schema's defined keys.
    """
    from specivo.services.metadata_schema_service import MetadataSchemaService

    project = await _get_project(session, user, project_key)
    await _require_permission(session, user, project.id, "manage_project")

    svc = MetadataSchemaService()
    existing = await svc.get_by_id(session, schema_id, project.id)
    schema_name = existing.name
    schema_tracker_id = existing.tracker_id
    await svc.delete_safe(session, existing)
    await _log_tool(
        session,
        user,
        AuditEvent.METADATA_SCHEMA_DELETED,
        "delete_metadata_schema",
        {
            "project_key": project.key,
            "schema_id": schema_id,
            "name": schema_name,
            "tracker_id": schema_tracker_id,
        },
        project_id=project.id,
    )
    return f"Deleted metadata schema id={schema_id} name='{schema_name}' from project {project.key}."


# ---------------------------------------------------------------------------
# Metadata (per-key set/delete/append/remove)
# ---------------------------------------------------------------------------


# Serialized metadata blob size cap (bytes of JSON output).  Enforced
# after the op is applied — prevents unbounded growth via repeated
# ``append`` calls.
_METADATA_MAX_BYTES = 16 * 1024


def _detect_lossy_number(value: Any) -> str | None:
    """Detect a numeric value that has lost precision via JSON parsing.

    When an MCP client emits an identifier-shaped token like ``49830031...e9999``
    without quoting it, the JSON-RPC layer parses it as a Python ``float`` (often
    ``inf`` or scientific notation). The original lexical form is unrecoverable.
    Return a human-readable description of the offending value, or ``None`` if
    the value is safe.
    """
    import math

    if isinstance(value, list):
        for item in value:
            offender = _detect_lossy_number(item)
            if offender is not None:
                return offender
        return None
    if isinstance(value, float):
        if math.isinf(value) or math.isnan(value):
            return repr(value)
        # Floats serialized in scientific notation are the exact ambiguity we
        # care about — a quoted string would never have produced this shape.
        import json as _json

        rendered = _json.dumps(value)
        if "e" in rendered.lower():
            return rendered
    return None


async def _metadata(  # noqa: C901, PLR0912, PLR0915
    session: AsyncSession,
    user: User,
    target_ref: str,
    key: str,
    op: str,
    value: Any = None,
) -> str:
    """Per-key metadata mutation dispatched through the target registry.

    Supported ops:

    * ``set``     -- ``metadata[key] = value`` (any JSON value).
    * ``get``     -- return ``metadata[key]`` as a JSON-serialized string,
      or the literal ``"(not set)"`` if the key is missing.  Read-only.
    * ``delete``  -- pop *key*; silent no-op if missing.
    * ``append``  -- append to a list at *key*.  Scalar values push one
      element; list values extend.  Missing key creates a new list.
      Errors if the existing value is not a list.
    * ``remove``  -- remove matching items from a list at *key*.  Scalar
      removes a single matching element; list removes each matching
      element.  Errors if the existing value is not a list.

    Every refusal (bad op, wrong type, stale version, oversize blob,
    permission denied, unknown target, missing entity) raises, so the
    agent gets a tool error and nothing is written.
    """
    import json

    from sqlalchemy.orm.exc import StaleDataError

    from specivo.core.metadata_targets import get_metadata_target_registry

    registry = get_metadata_target_registry()
    scheme, ref = registry.parse_ref(target_ref)
    target = registry.get(scheme)
    if target is None:
        known = ", ".join(registry.schemes()) or "(none)"
        raise ValidationError(f"unknown metadata target scheme '{scheme}'. Known schemes: {known}", field="target_ref")

    if not key:
        raise ValidationError("key must be a non-empty string", field="key")

    valid_ops = {"set", "get", "delete", "append", "remove"}
    if op not in valid_ops:
        raise ValidationError(f"invalid op '{op}'. Must be one of: {', '.join(sorted(valid_ops))}", field="op")

    if op in {"set", "append", "remove"}:
        offender = _detect_lossy_number(value)
        if offender is not None:
            raise ValidationError(
                f"value {offender} arrived as a number with lost precision "
                "(scientific notation, inf, or nan). Identifier-like inputs "
                "(commit hashes, PR ids, tokens) must be passed as JSON strings — "
                'quote the value, e.g. "4983e31..." instead of 4983e31...',
                field="value",
            )

    # Resolve & permission check
    try:
        entity = await target.resolve(session, ref, user)
    except NotFoundError:
        raise NotFoundError(f"{scheme} '{ref}' not found") from None
    required_permission = target.read_permission if op == "get" else target.permission
    await _require_permission(session, user, target.project_id_of(entity), required_permission)

    metadata = target.get_metadata(entity)

    # Read-only path — return without mutating, flushing, or journaling.
    if op == "get":
        # Overlay project-derived (computed) metadata for issues so the
        # effective value is returned even though it is never stored.
        if scheme == "issue":
            metadata = merge_computed(metadata, await load_project_settings(session, target.project_id_of(entity)))
        if key not in metadata:
            return "(not set)"
        try:
            return json.dumps(metadata[key], default=str, ensure_ascii=False)
        except TypeError:
            raise ValidationError(f"value at key '{key}' is not JSON-serializable", field="key") from None

    # Apply op to a local copy.
    if op == "set":
        metadata[key] = value
    elif op == "delete":
        metadata.pop(key, None)
    elif op == "append":
        existing = metadata.get(key)
        if existing is None:
            metadata[key] = list(value) if isinstance(value, list) else [value]
        elif isinstance(existing, list):
            if isinstance(value, list):
                existing.extend(value)
            else:
                existing.append(value)
        else:
            raise ValidationError(
                f"cannot append to key '{key}': existing value is {type(existing).__name__}, expected array",
                field="key",
            )
    elif op == "remove":
        existing = metadata.get(key)
        if existing is None:
            # silent no-op — nothing to remove
            pass
        elif isinstance(existing, list):
            drop = value if isinstance(value, list) else [value]
            metadata[key] = [item for item in existing if item not in drop]
        else:
            raise ValidationError(
                f"cannot remove from key '{key}': existing value is {type(existing).__name__}, expected array",
                field="key",
            )

    # Size cap (serialized JSON, post-op)
    try:
        serialized = json.dumps(metadata, default=str)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"metadata value is not JSON-serializable: {exc}", field="value") from None
    if len(serialized.encode("utf-8")) > _METADATA_MAX_BYTES:
        raise ValidationError(
            f"metadata blob would exceed {_METADATA_MAX_BYTES} bytes after this operation", field="value"
        )

    # Persist through the target. A ValidationError or ConflictError from the
    # target reaches the agent as a tool error as it is.
    try:
        updated_entity = await target.set_metadata(session, entity, metadata, user)
    except StaleDataError:
        raise ConflictError("issue was modified by another request, retry") from None
    await session.flush()

    await _log_tool(
        session,
        user,
        AuditEvent.ISSUE_UPDATED,
        "metadata",
        {"target_ref": target_ref, "key": key, "op": op},
        project_id=target.project_id_of(updated_entity),
    )
    display = target.display_ref(updated_entity)
    return f"Updated metadata on {display}: {op} key={key}"
