"""Load logged time.

Nearly a direct mapping, with two things to get right.

Redmine stores hours as a float with far more precision than anyone entered —
a value typed as 7.5 can come back as 7.499999999999999 — while Specivo stores
two decimal places. Values are rounded, and a rounding that discards precision
the source really held is reported, because on a billable instance that is
somebody's invoice. Float artefacts are not reported: nobody typed them.

Redmine also records both whose time an entry is and who logged it. Specivo
keeps only the former, so an entry logged by a manager on someone else's behalf
keeps the person it belongs to.
"""

from __future__ import annotations

import logging
from decimal import ROUND_HALF_UP, Decimal

from specivo.core.exceptions import AppError
from specivo.importers.core.backdate import backdate
from specivo.importers.core.ir import EntityType, IRTimeEntry
from specivo.importers.core.pipeline import PhaseContext
from specivo.importers.load.user_loader import IMPORT_ACCOUNT_STATE_KEY, ensure_import_account
from specivo.models.time_entry import TimeEntry
from specivo.models.user import User
from specivo.schemas.time_entry import TimeEntryCreate
from specivo.services.time_entry_service import TimeEntryService

logger = logging.getLogger(__name__)

# Specivo stores two decimal places; anything finer was never entered by hand.
_CENT = Decimal("0.01")

# Rounding to two places can never move a value by more than half a cent, so a
# threshold in cents would never fire. What distinguishes a value somebody
# actually typed from a float artefact is the size of the difference: Redmine
# returns 7.5 as 7.499999999999999, which is off by about 1e-15, while a third
# decimal place that was really recorded is off by thousandths.
_FLOAT_NOISE = Decimal("1e-9")

_time_entry_service = TimeEntryService()


async def load_time_entries(ctx: PhaseContext) -> None:
    """Create every time entry in the selected projects."""
    await ensure_import_account(ctx)

    for project_ref in ctx.project_refs:
        project_id = await ctx.id_map.get(ctx.session, EntityType.PROJECT, project_ref)
        if project_id is None:
            continue

        async for ir in ctx.adapter.extract_time_entries(project_ref):
            if await ctx.id_map.get(ctx.session, EntityType.TIME_ENTRY, ir.source_ref):
                ctx.summary.record_skipped(EntityType.TIME_ENTRY)
                continue
            await _create_entry(ctx, project_id, ir)


async def _create_entry(ctx: PhaseContext, project_id: int, ir: IRTimeEntry) -> TimeEntry | None:
    """Create one time entry, resolving its activity, issue and owner."""
    hours = ir.hours.quantize(_CENT, rounding=ROUND_HALF_UP)
    if abs(hours - ir.hours) > _FLOAT_NOISE:
        ctx.warn(
            "Logged hours were rounded to two decimal places",
            entry=ir.source_ref,
            source=str(ir.hours),
            stored=str(hours),
        )
    if hours <= 0:
        # The schema requires a positive duration, and inventing one would put
        # time on somebody's record that they never logged.
        ctx.warn("Time entry rounds to zero hours; skipped", entry=ir.source_ref, source=str(ir.hours))
        return None

    activity_id = await ctx.id_map.get(ctx.session, EntityType.ACTIVITY, ir.activity_ref)
    if activity_id is None:
        ctx.warn("Time entry's activity was not imported; skipped", entry=ir.source_ref)
        return None

    user = await _resolve_user(ctx, ir.user_ref)
    issue_id = await ctx.id_map.get(ctx.session, EntityType.ISSUE, ir.issue_ref)
    if ir.issue_ref and issue_id is None:
        # Redmine allows time against an issue in another project; keep the
        # entry on the project rather than dropping it.
        ctx.warn("Time entry's issue was not imported; kept against the project", entry=ir.source_ref)

    try:
        entry = await _time_entry_service.create(
            ctx.session,
            project_id,
            TimeEntryCreate(
                issue_id=issue_id,
                activity_id=activity_id,
                hours=hours,
                comments=ir.comments,
                spent_on=ir.spent_on,
            ),
            user,
        )
    except AppError as exc:
        ctx.warn("Time entry was rejected and skipped", entry=ir.source_ref, reason=str(exc))
        return None

    await backdate(ctx.session, TimeEntry, entry.id, created_at=ir.created_at, updated_at=ir.updated_at)
    await ctx.id_map.put(ctx.session, EntityType.TIME_ENTRY, ir.source_ref, "time_entries", entry.id)
    ctx.summary.record_created(EntityType.TIME_ENTRY)
    ctx.tick()
    return entry


async def _resolve_user(ctx: PhaseContext, user_ref: str | None) -> User:
    """Return whose time this is, falling back to the import account."""
    user_id = await ctx.id_map.get(ctx.session, EntityType.USER, user_ref)
    if user_id is not None:
        user = await ctx.session.get(User, user_id)
        if user is not None:
            return user
    account = ctx.state.get(IMPORT_ACCOUNT_STATE_KEY)
    return account if account is not None else await ensure_import_account(ctx)
