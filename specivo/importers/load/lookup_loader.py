"""Load instance-wide lookups: statuses, trackers, priorities, activities, roles.

Every project and issue points at these, so they land first.

The recurring decision is what to do when the source has a lookup whose name a
Specivo installation already uses. A fresh Specivo is seeded with New, In
Progress, Bug, Feature and so on, and so is nearly every Redmine, so collisions
are the norm rather than the exception. Three different answers, each forced by
what the schema allows:

* **Statuses, trackers and priorities** get a second row by default, because
  merging two same-named lookups quietly merges their meaning too — an
  imported "Closed" may not be the "Closed" already there. Every collision is
  reported, and ``--merge-duplicate-statuses`` switches to reusing the existing
  row once the operator has seen the list and agrees.
* **Activities and roles** are always reused: both names are unique in
  Specivo's schema, so a second row is not possible.
* **Roles** are additionally never created with permissions. Redmine's
  permission vocabulary is its own, so an unmatched role arrives empty and is
  listed for an administrator to fill in — an empty role grants nothing, which
  is the safe direction to be wrong in.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.importers.core.ir import EntityType, IRActivity, IRPriority, IRRole, IRStatus, IRTracker
from specivo.importers.core.pipeline import PhaseContext
from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.role import Role
from specivo.models.time_entry import TimeEntryActivity

logger = logging.getLogger(__name__)

# Note listing roles that arrived with no permissions.
NOTE_ROLES_NEED_PERMISSIONS = "roles_need_permissions"


async def load_lookups(ctx: PhaseContext) -> None:
    """Load every instance-wide lookup, statuses before the trackers that use them."""
    lookups = await ctx.adapter.extract_lookups()

    await _load_statuses(ctx, lookups.statuses)
    await _load_trackers(ctx, lookups.trackers)
    await _load_priorities(ctx, lookups.priorities)
    await _load_activities(ctx, lookups.activities)
    await _load_roles(ctx, lookups.roles)


async def _find_by_name(session: AsyncSession, model: Any, name: str) -> Any | None:
    """Return the row whose name matches *name*, ignoring case and padding."""
    stmt = select(model).where(func.lower(model.name) == name.strip().lower()).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none()


async def _has_default(session: AsyncSession, model: Any) -> bool:
    """Whether the instance already has a default row for this lookup."""
    stmt = select(model.id).where(model.is_default.is_(True)).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def _load_statuses(ctx: PhaseContext, statuses: list[IRStatus]) -> None:
    for ir in statuses:
        if await ctx.id_map.get(ctx.session, EntityType.STATUS, ir.source_ref):
            ctx.summary.record_skipped(EntityType.STATUS)
            continue

        existing = await _find_by_name(ctx.session, IssueStatus, ir.name)
        if existing is not None and ctx.options.merge_duplicate_statuses:
            await _map(ctx, EntityType.STATUS, ir.source_ref, "issue_statuses", existing.id)
            if existing.category != ir.category:
                ctx.warn(
                    "Reused an existing status whose category differs from the source",
                    status=ir.name,
                    existing=existing.category,
                    source=ir.category,
                )
            continue

        if existing is not None:
            ctx.warn(
                "A status of this name already exists; a second one was created."
                " Re-run with --merge-duplicate-statuses to reuse it instead",
                status=ir.name,
            )

        row = IssueStatus(
            name=ir.name,
            category=ir.category,
            position=ir.position or 1,
            default_done_ratio=ir.default_done_ratio,
        )
        await _create(ctx, row, EntityType.STATUS, ir.source_ref, "issue_statuses")


async def _load_trackers(ctx: PhaseContext, trackers: list[IRTracker]) -> None:
    for ir in trackers:
        if await ctx.id_map.get(ctx.session, EntityType.TRACKER, ir.source_ref):
            ctx.summary.record_skipped(EntityType.TRACKER)
            continue

        # A tracker must point at a status, and its own default may be one the
        # source disabled or one this run did not import.
        default_status_id = await ctx.id_map.get(ctx.session, EntityType.STATUS, ir.default_status_ref)
        if default_status_id is None:
            default_status_id = await _fallback_status_id(ctx.session)
            if ir.default_status_ref is not None:
                ctx.warn(
                    "Tracker's default status was not imported; fell back to the first status",
                    tracker=ir.name,
                )

        existing = await _find_by_name(ctx.session, Tracker, ir.name)
        if existing is not None and ctx.options.merge_duplicate_statuses:
            await _map(ctx, EntityType.TRACKER, ir.source_ref, "trackers", existing.id)
            continue

        if existing is not None:
            ctx.warn(
                "A tracker of this name already exists; a second one was created."
                " Re-run with --merge-duplicate-statuses to reuse it instead",
                tracker=ir.name,
            )

        row = Tracker(
            name=ir.name,
            default_status_id=default_status_id,
            is_in_roadmap=ir.is_in_roadmap,
            position=ir.position or 1,
            description=ir.description,
            disabled_core_fields=list(ir.disabled_core_fields),
        )
        await _create(ctx, row, EntityType.TRACKER, ir.source_ref, "trackers")


async def _fallback_status_id(session: AsyncSession) -> int | None:
    """Return the lowest-positioned status, used when a default cannot be resolved."""
    stmt = select(IssueStatus.id).order_by(IssueStatus.position, IssueStatus.id).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none()


async def _load_priorities(ctx: PhaseContext, priorities: list[IRPriority]) -> None:
    for ir in priorities:
        if await ctx.id_map.get(ctx.session, EntityType.PRIORITY, ir.source_ref):
            ctx.summary.record_skipped(EntityType.PRIORITY)
            continue

        existing = await _find_by_name(ctx.session, IssuePriority, ir.name)
        if existing is not None and ctx.options.merge_duplicate_statuses:
            await _map(ctx, EntityType.PRIORITY, ir.source_ref, "issue_priorities", existing.id)
            continue

        if existing is not None:
            ctx.warn(
                "A priority of this name already exists; a second one was created."
                " Re-run with --merge-duplicate-statuses to reuse it instead",
                priority=ir.name,
            )

        # Two defaults would make which one applies arbitrary, so an existing
        # default is left in place rather than silently replaced.
        is_default = ir.is_default
        if is_default and await _has_default(ctx.session, IssuePriority):
            is_default = False
            ctx.warn(
                "Source priority is the default but this instance already has one; imported as non-default",
                priority=ir.name,
            )

        row = IssuePriority(
            name=ir.name,
            position=ir.position or 1,
            is_default=is_default,
            active=ir.active,
        )
        await _create(ctx, row, EntityType.PRIORITY, ir.source_ref, "issue_priorities")


async def _load_activities(ctx: PhaseContext, activities: list[IRActivity]) -> None:
    for ir in activities:
        if await ctx.id_map.get(ctx.session, EntityType.ACTIVITY, ir.source_ref):
            ctx.summary.record_skipped(EntityType.ACTIVITY)
            continue

        # time_entry_activities.name is unique, so a same-named activity is
        # always reused whatever the merge flag says.
        existing = await _find_by_name(ctx.session, TimeEntryActivity, ir.name)
        if existing is not None:
            await _map(ctx, EntityType.ACTIVITY, ir.source_ref, "time_entry_activities", existing.id)
            continue

        is_default = ir.is_default
        if is_default and await _has_default(ctx.session, TimeEntryActivity):
            is_default = False

        row = TimeEntryActivity(name=ir.name, is_default=is_default, active=ir.active)
        await _create(ctx, row, EntityType.ACTIVITY, ir.source_ref, "time_entry_activities")


async def _load_roles(ctx: PhaseContext, roles: list[IRRole]) -> None:
    for ir in roles:
        if await ctx.id_map.get(ctx.session, EntityType.ROLE, ir.source_ref):
            ctx.summary.record_skipped(EntityType.ROLE)
            continue

        # roles.name is unique, so same-named roles are always reused. The
        # builtin non-member and anonymous roles are matched on that flag
        # instead, since either system may name them differently.
        existing = None
        if ir.builtin:
            stmt = select(Role).where(Role.builtin == ir.builtin).limit(1)
            existing = (await ctx.session.execute(stmt)).scalar_one_or_none()
        if existing is None:
            existing = await _find_by_name(ctx.session, Role, ir.name)

        if existing is not None:
            await _map(ctx, EntityType.ROLE, ir.source_ref, "roles", existing.id)
            continue

        row = Role(name=ir.name, builtin=ir.builtin, permissions=[], assignable=True)
        await _create(ctx, row, EntityType.ROLE, ir.source_ref, "roles")
        ctx.summary.add_note(NOTE_ROLES_NEED_PERMISSIONS, ir.name)


async def _create(ctx: PhaseContext, row: Any, entity_type: EntityType, source_ref: str, table: str) -> None:
    """Persist *row*, map it, and count it."""
    ctx.session.add(row)
    await ctx.session.flush()
    await ctx.id_map.put(ctx.session, entity_type, source_ref, table, row.id)
    ctx.summary.record_created(entity_type)
    ctx.tick()


async def _map(ctx: PhaseContext, entity_type: EntityType, source_ref: str, table: str, row_id: int) -> None:
    """Map a source entity onto a row Specivo already had."""
    await ctx.id_map.put(ctx.session, entity_type, source_ref, table, row_id)
    ctx.summary.record_reused(entity_type)
    ctx.tick()
