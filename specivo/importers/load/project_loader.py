"""Load projects and everything scoped to one: versions, categories,
memberships and custom-field schemas.

Most of this is a direct mapping. Three things are not.

**Projects need a key that Redmine never had.** Specivo identifies an issue as
``ACME-15``; Redmine identifies it as ``#15`` and has no per-project prefix at
all. A key is derived from the project identifier, which is the closest thing
Redmine has to a short name, and the operator can override any of them. It is
the one mapping worth reviewing before a large import, so every derived key is
reported.

**Group memberships are flattened.** Specivo cannot hang roles off a group, so
a group's grant is expanded into an identical grant for each of its members.
The resulting permissions match the source exactly; what is lost is the
knowledge that they came from a group.

**Custom fields become metadata schemas per project.** A Redmine custom field
is instance-wide; a Specivo metadata schema belongs to a project and optionally
a tracker. Fields are therefore grouped by the scope they apply to, so a project
gets one schema per tracker that has fields rather than one schema per field.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Literal, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.constants import MAX_PROJECT_DEPTH
from specivo.importers.core.backdate import backdate
from specivo.importers.core.ir import EntityType, IRCustomField, IRProject, PrincipalKind
from specivo.importers.core.pipeline import PhaseContext
from specivo.importers.load.user_loader import (
    GROUP_MEMBERS_STATE_KEY,
    ensure_import_account,
)
from specivo.models.lookups import IssueCategory
from specivo.models.project import Project
from specivo.models.version import Version
from specivo.schemas.metadata_schema import MetadataSchemaCreate
from specivo.schemas.project import ProjectCreate
from specivo.schemas.version import VersionCreate
from specivo.services.metadata_schema_service import MetadataSchemaService
from specivo.services.project_service import ProjectService
from specivo.services.version_service import VersionService

logger = logging.getLogger(__name__)

VersionStatus = Literal["open", "locked", "closed"]
VersionSharing = Literal["none", "descendants", "hierarchy", "tree", "system"]

# Report sections.
NOTE_PROJECT_KEYS = "project_keys_assigned"
NOTE_MODULES_DROPPED = "project_modules_without_an_equivalent"

# Where the derived custom-field keys are parked for the issue phase.
CUSTOM_FIELD_KEYS_STATE_KEY = "custom_field_keys"

# Versions are created open and their real status applied once every issue
# that targets them exists. See restore_version_statuses.
VERSION_STATUS_STATE_KEY = "version_statuses"

# projects.key must be 2 to 128 characters, start with a letter, and hold only
# letters and digits.
_KEY_ALLOWED_RE = re.compile(r"[^A-Z0-9]")
_MAX_KEY = 128
_MAX_IDENTIFIER = 100

_project_service = ProjectService()
_version_service = VersionService()
_metadata_schema_service = MetadataSchemaService()


# --------------------------------------------------------------------------
# Projects
# --------------------------------------------------------------------------


async def load_projects(ctx: PhaseContext) -> None:
    """Create every project, parents before children.

    The adapter yields them in an order where a parent always precedes its
    children, so a child's parent is mapped by the time it is reached.
    """
    # Projects are attributed to the import account, so it has to exist first.
    await ensure_import_account(ctx)

    # --project selects a subtree, not a single project: importing a parent
    # without its children would leave the children's issues unreachable.
    wanted = set(ctx.options.project_refs or [])
    imported: list[str] = []
    depths: dict[str, int] = {}
    parents: dict[str, str | None] = {}

    async for ir in ctx.adapter.extract_projects():
        if wanted and ir.source_ref not in wanted and ir.parent_ref not in imported:
            continue

        parents[ir.source_ref] = ir.parent_ref
        existing_id = await ctx.id_map.get(ctx.session, EntityType.PROJECT, ir.source_ref)
        if existing_id is not None:
            ctx.summary.record_skipped(EntityType.PROJECT)
            imported.append(ir.source_ref)
            depths[ir.source_ref] = await _depth_of(ctx.session, existing_id)
            continue

        project = await _create_project(ctx, ir, depths, parents)
        if project is None:
            continue
        imported.append(ir.source_ref)
        ctx.tick()

    ctx.project_refs = imported


async def _create_project(
    ctx: PhaseContext,
    ir: IRProject,
    depths: dict[str, int],
    parents: dict[str, str | None],
) -> Project | None:
    """Create one project, resolving its parent, key and identifier."""
    parent_ref = ir.parent_ref
    parent_key: str | None = None

    if parent_ref is not None:
        parent_id = await ctx.id_map.get(ctx.session, EntityType.PROJECT, parent_ref)
        if parent_id is None:
            # The parent was outside the selected scope, so this project becomes
            # a root rather than being dropped along with it.
            ctx.warn("Parent project was not imported; imported as a root project", project=ir.identifier)
            parent_ref = None
        else:
            parent_depth = depths.get(parent_ref, 1)
            if parent_depth >= MAX_PROJECT_DEPTH:
                if not ctx.options.flatten_excess_depth:
                    ctx.warn(
                        "Project is nested deeper than Specivo allows and was skipped."
                        " Re-run with --flatten-excess-depth to import it higher up",
                        project=ir.identifier,
                        depth=parent_depth + 1,
                        max_depth=MAX_PROJECT_DEPTH,
                    )
                    return None
                parent_ref = _shallowest_allowed(parent_ref, depths, parents)
                ctx.warn(
                    "Project is nested too deep; re-parented higher up to keep it",
                    project=ir.identifier,
                    max_depth=MAX_PROJECT_DEPTH,
                )
                parent_id = await ctx.id_map.get(ctx.session, EntityType.PROJECT, parent_ref) if parent_ref else None
                if parent_id is None:
                    parent_ref = None
            parent = await ctx.session.get(Project, parent_id)
            parent_key = parent.key if parent else None

    identifier = await _unique_identifier(ctx, ir.identifier or f"project-{ir.source_ref}")
    key = await _unique_key(ctx, ir, identifier)

    project = await _project_service.create(
        ctx.session,
        ProjectCreate(
            name=ir.name or identifier,
            identifier=identifier,
            key=key,
            description=ir.description,
            parent_key=parent_key,
            is_public=ir.is_public,
            modules=ir.modules or None,
        ),
        creator_user=await ensure_import_account(ctx),
    )

    # ProjectCreate has no status field: a closed or archived project can only
    # be created open and then set.
    if ir.status != 1:
        project.status = ir.status
        await ctx.session.flush()

    await backdate(ctx.session, Project, project.id, created_at=ir.created_at, updated_at=ir.updated_at)
    await ctx.id_map.put(ctx.session, EntityType.PROJECT, ir.source_ref, "projects", project.id)

    depths[ir.source_ref] = (depths.get(parent_ref, 0) + 1) if parent_ref else 1
    ctx.summary.record_created(EntityType.PROJECT)
    ctx.summary.add_note(NOTE_PROJECT_KEYS, f"{ir.identifier or ir.source_ref} -> {key}")

    dropped = _dropped_modules(ctx, ir.source_ref)
    if dropped:
        ctx.summary.add_note(NOTE_MODULES_DROPPED, f"{identifier}: {', '.join(dropped)}")
    return project


def _dropped_modules(ctx: PhaseContext, project_ref: str) -> list[str]:
    """Return modules the adapter could not map, when it tracks them."""
    reader = getattr(ctx.adapter, "dropped_modules", None)
    return list(reader(project_ref)) if callable(reader) else []


def _shallowest_allowed(
    parent_ref: str,
    depths: dict[str, int],
    parents: dict[str, str | None],
) -> str | None:
    """Return the deepest ancestor a child may still hang off.

    Walks up until the depth leaves room for one more level. Only reached with
    ``--flatten-excess-depth``, where the operator has said they would rather
    have the project than the exact shape of the tree.
    """
    candidate: str | None = parent_ref
    while candidate is not None and depths.get(candidate, 1) >= MAX_PROJECT_DEPTH:
        candidate = parents.get(candidate)
    return candidate


async def _depth_of(session: AsyncSession, project_id: int) -> int:
    """Return a project's depth from its ltree path."""
    project = await session.get(Project, project_id)
    if project is None or not project.path:
        return 1
    return str(project.path).count(".") + 1


async def _unique_identifier(ctx: PhaseContext, identifier: str) -> str:
    """Return a free project identifier, suffixed if the wanted one is taken."""
    base = re.sub(r"[^a-z0-9-]+", "-", identifier.strip().lower()).strip("-")[:_MAX_IDENTIFIER]
    if not base or not base[0].isalpha():
        base = f"p-{base}" if base else f"project-{ctx.summary.created[EntityType.PROJECT] + 1}"

    candidate = base
    suffix = 2
    while await _identifier_taken(ctx.session, candidate):
        tail = f"-{suffix}"
        candidate = f"{base[: _MAX_IDENTIFIER - len(tail)]}{tail}"
        suffix += 1
    if candidate != base:
        ctx.warn("Project identifier already taken; imported under a suffixed one", wanted=base, identifier=candidate)
    return candidate


async def _identifier_taken(session: AsyncSession, identifier: str) -> bool:
    stmt = select(Project.id).where(Project.identifier == identifier).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def _unique_key(ctx: PhaseContext, ir: IRProject, identifier: str) -> str:
    """Return the issue-key prefix for a project.

    Redmine has no such concept, so it is derived from the identifier unless the
    operator supplied one. Every result is reported, because this is the mapping
    people most often want to correct.
    """
    override = ctx.options.project_key_map.get(ir.identifier) or ctx.options.project_key_map.get(ir.source_ref)
    base = _key_from(override or identifier)

    candidate = base
    suffix = 2
    while await _key_taken(ctx.session, candidate):
        tail = str(suffix)
        candidate = f"{base[: _MAX_KEY - len(tail)]}{tail}"
        suffix += 1
    if candidate != base and override:
        ctx.warn("Requested project key was taken; used a suffixed one", wanted=base, key=candidate)
    return candidate


def _key_from(value: str) -> str:
    """Shape *value* into something the project key column accepts."""
    key = _KEY_ALLOWED_RE.sub("", value.upper())[:_MAX_KEY]
    if not key or not key[0].isalpha():
        key = f"P{key}"[:_MAX_KEY]
    if len(key) < 2:
        key = f"{key}X"
    return key


async def _key_taken(session: AsyncSession, key: str) -> bool:
    stmt = select(Project.id).where(Project.key == key).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none() is not None


# --------------------------------------------------------------------------
# Versions and categories
# --------------------------------------------------------------------------


async def load_project_lookups(ctx: PhaseContext) -> None:
    """Create each project's versions and issue categories."""
    for project_ref in ctx.project_refs:
        project = await _project_for(ctx, project_ref)
        if project is None:
            continue

        async for version_ir in ctx.adapter.extract_versions(project_ref):
            if await ctx.id_map.get(ctx.session, EntityType.VERSION, version_ir.source_ref):
                ctx.summary.record_skipped(EntityType.VERSION)
                continue
            version = await _version_service.create(
                ctx.session,
                project,
                VersionCreate(
                    name=version_ir.name,
                    description=version_ir.description,
                    # Created open whatever the source says: Specivo refuses to
                    # put an issue on a locked or closed version, and the source
                    # is full of issues sitting on exactly those. The real
                    # status is applied once the issues are in.
                    status="open",
                    effective_date=version_ir.effective_date,
                    sharing=_version_sharing(ctx, version_ir.sharing, version_ir.name),
                    wiki_page_title=version_ir.wiki_page_title,
                ),
            )
            await backdate(
                ctx.session,
                Version,
                version.id,
                created_at=version_ir.created_at,
                updated_at=version_ir.updated_at,
            )
            await ctx.id_map.put(ctx.session, EntityType.VERSION, version_ir.source_ref, "versions", version.id)

            intended = _version_status(ctx, version_ir.status, version_ir.name)
            if intended != "open":
                ctx.state.setdefault(VERSION_STATUS_STATE_KEY, {})[version.id] = intended

            ctx.summary.record_created(EntityType.VERSION)
            ctx.tick()

        async for category_ir in ctx.adapter.extract_categories(project_ref):
            if await ctx.id_map.get(ctx.session, EntityType.CATEGORY, category_ir.source_ref):
                ctx.summary.record_skipped(EntityType.CATEGORY)
                continue
            category = IssueCategory(
                project_id=project.id,
                name=category_ir.name,
                assigned_to_id=await ctx.id_map.get(ctx.session, EntityType.USER, category_ir.assigned_to_ref),
            )
            ctx.session.add(category)
            await ctx.session.flush()
            await ctx.id_map.put(
                ctx.session, EntityType.CATEGORY, category_ir.source_ref, "issue_categories", category.id
            )
            ctx.summary.record_created(EntityType.CATEGORY)
            ctx.tick()


# Both systems use the same vocabulary here, so an unrecognised value means the
# source data is odd rather than that the mapping is missing. Falling back to
# the safest option keeps one strange row from ending the import.
_VERSION_STATUSES: tuple[VersionStatus, ...] = ("open", "locked", "closed")
_VERSION_SHARINGS: tuple[VersionSharing, ...] = ("none", "descendants", "hierarchy", "tree", "system")


def _version_status(ctx: PhaseContext, value: str, name: str) -> VersionStatus:
    """Return a valid version status, defaulting to open."""
    if value in _VERSION_STATUSES:
        return cast(VersionStatus, value)
    ctx.warn("Version has an unrecognised status; imported as open", version=name, status=value)
    return "open"


def _version_sharing(ctx: PhaseContext, value: str, name: str) -> VersionSharing:
    """Return a valid sharing mode, defaulting to none.

    Defaulting to no sharing is the narrow choice: the version stays visible
    where it was defined rather than appearing in projects it should not.
    """
    if value in _VERSION_SHARINGS:
        return cast(VersionSharing, value)
    ctx.warn("Version has an unrecognised sharing mode; imported as unshared", version=name, sharing=value)
    return "none"


# --------------------------------------------------------------------------
# Memberships
# --------------------------------------------------------------------------


async def load_memberships(ctx: PhaseContext) -> None:
    """Grant project roles, expanding group memberships into individual ones."""
    group_members: dict[str, list[str]] = ctx.state.get(GROUP_MEMBERS_STATE_KEY, {})

    for project_ref in ctx.project_refs:
        project = await _project_for(ctx, project_ref)
        if project is None:
            continue

        async for ir in ctx.adapter.extract_memberships(project_ref):
            role_ids = [
                role_id
                for role_id in [
                    await ctx.id_map.get(ctx.session, EntityType.ROLE, role_ref) for role_ref in ir.role_refs
                ]
                if role_id is not None
            ]
            if not role_ids:
                ctx.warn("Membership has no role that was imported; skipped", project=project.key)
                continue

            if ir.principal_kind is PrincipalKind.USER:
                principal_refs = [ir.principal_ref]
            else:
                principal_refs = group_members.get(ir.principal_ref, [])
                if not principal_refs:
                    ctx.warn("Group membership has no members to expand to", project=project.key)
                    continue

            for user_ref in principal_refs:
                user_id = await ctx.id_map.get(ctx.session, EntityType.USER, user_ref)
                if user_id is None:
                    ctx.warn("Membership refers to a user that was not imported; skipped", project=project.key)
                    continue
                # add_member merges roles into an existing membership, so a user
                # who is both a direct member and in a group ends up with the
                # union of both grants, which is what Redmine gave them.
                await _project_service.add_member(ctx.session, project, user_id, role_ids)
                ctx.summary.record_created(EntityType.MEMBERSHIP)
                ctx.tick()


# --------------------------------------------------------------------------
# Custom field schemas
# --------------------------------------------------------------------------


async def load_custom_field_schemas(ctx: PhaseContext) -> None:
    """Turn Redmine custom fields into per-project metadata schemas.

    A field applying to several trackers produces one schema per tracker, since
    a schema carries a single tracker. A field with no tracker link becomes a
    project-wide schema, so its historical values still validate.
    """
    fields = [field async for field in ctx.adapter.extract_custom_fields()]
    ctx.state[CUSTOM_FIELD_KEYS_STATE_KEY] = {field.source_ref: field for field in fields}
    if not fields:
        return

    for project_ref in ctx.project_refs:
        project = await _project_for(ctx, project_ref)
        if project is None:
            continue

        applicable = [field for field in fields if field.is_for_all or project_ref in field.project_refs]
        if not applicable:
            continue

        by_tracker: dict[str | None, list[IRCustomField]] = {}
        for field in applicable:
            if not field.tracker_refs:
                by_tracker.setdefault(None, []).append(field)
            for tracker_ref in field.tracker_refs:
                by_tracker.setdefault(tracker_ref, []).append(field)

        for scope_ref, group in by_tracker.items():
            await _create_schema(ctx, project, project_ref, scope_ref, group)


async def _create_schema(
    ctx: PhaseContext,
    project: Project,
    project_ref: str,
    tracker_ref: str | None,
    fields: list[IRCustomField],
) -> None:
    """Create one metadata schema covering *fields* for a project and tracker."""
    source_id = f"{project_ref}:{tracker_ref or 'all'}"
    if await ctx.id_map.get(ctx.session, EntityType.CUSTOM_FIELD, source_id):
        ctx.summary.record_skipped(EntityType.CUSTOM_FIELD)
        return

    tracker_id = await ctx.id_map.get(ctx.session, EntityType.TRACKER, tracker_ref) if tracker_ref else None
    if tracker_ref and tracker_id is None:
        ctx.warn("Custom fields reference a tracker that was not imported; scoped project-wide instead")

    properties = {field.key: field.json_schema for field in fields}
    required = sorted({field.key for field in fields if field.is_required})
    definition: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        definition["required"] = required

    name = "Imported fields" if tracker_id is None else f"Imported fields ({await _tracker_name(ctx, tracker_id)})"
    schema = await _metadata_schema_service.create(
        ctx.session,
        project.id,
        MetadataSchemaCreate(
            name=name,
            tracker_id=tracker_id,
            content_type="issue",
            description="Custom fields carried over from the imported tracker.",
            schema_definition=definition,
        ),
    )
    await ctx.id_map.put(ctx.session, EntityType.CUSTOM_FIELD, source_id, "metadata_schemas", schema.id)
    ctx.summary.record_created(EntityType.CUSTOM_FIELD)
    ctx.tick()


async def _tracker_name(ctx: PhaseContext, tracker_id: int) -> str:
    from specivo.models.lookups import Tracker

    tracker = await ctx.session.get(Tracker, tracker_id)
    return tracker.name if tracker else str(tracker_id)


async def restore_version_statuses(ctx: PhaseContext) -> None:
    """Lock or close the versions that were created open.

    Specivo refuses to put an issue on a locked or closed version, which is
    right for somebody filing one today and wrong for a migration: the source
    is full of issues sitting on versions that were locked years ago. Versions
    are therefore created open and set to their real status here, once every
    issue that targets them exists.
    """
    intended: dict[int, str] = ctx.state.get(VERSION_STATUS_STATE_KEY, {})
    if not intended:
        return

    for version_id, status in intended.items():
        version = await ctx.session.get(Version, version_id)
        if version is None:
            continue
        version.status = status
        ctx.tick()
    await ctx.session.flush()


async def _project_for(ctx: PhaseContext, project_ref: str) -> Project | None:
    """Return the Specivo project a source project maps to."""
    project_id = await ctx.id_map.get(ctx.session, EntityType.PROJECT, project_ref)
    if project_id is None:
        return None
    return await ctx.session.get(Project, project_id)
