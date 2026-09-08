"""Load issues, their history, relations and watchers.

Issues go through ``IssueService.create`` so the parts that are easy to get
subtly wrong — the per-project sequence number, the nested set, auto-watching —
are done the way the rest of the application does them. Search indexing is
suppressed and run once at the end of the import instead of per row.

History does not go through the journal service. That service exists to diff a
live issue against what it used to be, and works out old and new values by
inspection; a source system already recorded both, in order. Replaying that
through the service would mean mutating each issue back and forth to make it
produce the right diff. The rows are built directly instead — still ORM
objects, still in the phase transaction — and numbered in the order the changes
happened.

Two things are deliberately deferred to a later pass:

* Issue references inside descriptions and comments stay as the source wrote
  them until every issue exists, because a reference can point at an issue in a
  project imported later.
* Custom-field values that name a user or a version are stored as source ids
  and rewritten once those exist.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import AppError
from specivo.importers.core.backdate import backdate
from specivo.importers.core.converter import ContentConverter, ConversionContext
from specivo.importers.core.ir import EntityType, IRIssue, IRJournalEntry, IRRelation
from specivo.importers.core.pipeline import PhaseContext
from specivo.importers.load.user_loader import ensure_import_account
from specivo.models.issue import Issue
from specivo.models.journal import Journal, JournalDetail
from specivo.models.project import Project
from specivo.models.watcher import Watcher
from specivo.schemas.issue import IssueCreate
from specivo.services.issue_service import IssueService
from specivo.services.relation_service import RelationService

logger = logging.getLogger(__name__)

# Where the converter is parked so every phase converts markup the same way.
CONVERTER_STATE_KEY = "converter"
CONVERSION_CONTEXT_STATE_KEY = "conversion_context"

_issue_service = IssueService()
_relation_service = RelationService()


def converter_for(ctx: PhaseContext) -> tuple[ContentConverter, ConversionContext]:
    """Return the markup converter for this run, building it once."""
    converter = ctx.state.get(CONVERTER_STATE_KEY)
    if converter is None:
        from specivo.importers.redmine.textile_convert import RedmineTextileConverter

        converter = RedmineTextileConverter()
        ctx.state[CONVERTER_STATE_KEY] = converter
        ctx.state[CONVERSION_CONTEXT_STATE_KEY] = ConversionContext(
            source_format=getattr(ctx.adapter, "source_format", "textile")
        )
    return converter, ctx.state[CONVERSION_CONTEXT_STATE_KEY]


def convert_markup(ctx: PhaseContext, raw: str | None) -> str | None:
    """Convert one field of source markup, leaving empty values alone."""
    if not raw:
        return None
    converter, conversion_ctx = converter_for(ctx)
    converted: str = converter.convert(raw, conversion_ctx)
    return converted


# --------------------------------------------------------------------------
# Issues
# --------------------------------------------------------------------------


async def load_issues(ctx: PhaseContext) -> None:
    """Create every issue in the selected projects, parents before children."""
    await ensure_import_account(ctx)

    for project_ref in ctx.project_refs:
        project = await _project_for(ctx, project_ref)
        if project is None:
            continue

        async for ir in ctx.adapter.extract_issues(project_ref):
            if await ctx.id_map.get(ctx.session, EntityType.ISSUE, ir.source_ref):
                ctx.summary.record_skipped(EntityType.ISSUE)
                continue
            await _create_issue(ctx, project, ir)


async def _create_issue(ctx: PhaseContext, project: Project, ir: IRIssue) -> Issue | None:
    """Create one issue, resolving everything it points at."""
    tracker_id = await ctx.id_map.get(ctx.session, EntityType.TRACKER, ir.tracker_ref)
    status_id = await ctx.id_map.get(ctx.session, EntityType.STATUS, ir.status_ref)
    priority_id = await ctx.id_map.get(ctx.session, EntityType.PRIORITY, ir.priority_ref)
    if tracker_id is None:
        ctx.warn("Issue's tracker was not imported; issue skipped", issue=ir.source_ref)
        return None

    author = await _resolve_author(ctx, ir.author_ref)
    assigned_to_id = await ctx.id_map.get(ctx.session, EntityType.USER, ir.assigned_to_ref)
    if ir.assigned_to_ref and assigned_to_id is None:
        # Redmine can assign an issue to a group, which Specivo cannot express.
        ctx.warn("Issue is assigned to someone who was not imported; left unassigned", issue=ir.source_ref)

    parent_id = await ctx.id_map.get(ctx.session, EntityType.ISSUE, ir.parent_ref)
    if ir.parent_ref and parent_id is None:
        ctx.warn("Issue's parent was not imported; imported as a top-level issue", issue=ir.source_ref)

    metadata = {value.key: value.value for value in ir.custom_values}

    try:
        issue = await _issue_service.create(
            ctx.session,
            project,
            IssueCreate(
                project_key=project.key,
                tracker_id=tracker_id,
                subject=ir.subject or f"Imported issue {ir.source_ref}",
                description=convert_markup(ctx, ir.description),
                status_id=status_id,
                priority_id=priority_id,
                assigned_to_id=assigned_to_id,
                category_id=await ctx.id_map.get(ctx.session, EntityType.CATEGORY, ir.category_ref),
                parent_id=parent_id,
                start_date=ir.start_date,
                due_date=ir.due_date,
                estimated_hours=ir.estimated_hours,
                done_ratio=ir.done_ratio,
                fixed_version_id=await ctx.id_map.get(ctx.session, EntityType.VERSION, ir.fixed_version_ref),
                is_private=ir.is_private,
                metadata=metadata,
            ),
            author,
            skip_search_index=True,
        )
    except AppError as exc:
        ctx.warn("Issue could not be created; skipped", issue=ir.source_ref, reason=str(exc))
        return None

    await backdate(
        ctx.session,
        Issue,
        issue.id,
        created_at=ir.created_at,
        updated_at=ir.updated_at,
        closed_on=ir.closed_at,
    )
    # The service records a baseline journal for the description, which stands
    # for the issue's creation and should carry its date, not the import's.
    if ir.created_at is not None:
        await ctx.session.execute(
            update(Journal)
            .where(Journal.issue_id == issue.id)
            .values(created_at=ir.created_at)
            .execution_options(synchronize_session=False)
        )

    await ctx.id_map.put(ctx.session, EntityType.ISSUE, ir.source_ref, "issues", issue.id)
    ctx.summary.record_created(EntityType.ISSUE)
    ctx.tick()
    return issue


async def _resolve_author(ctx: PhaseContext, author_ref: str | None):
    """Return the issue's author, falling back to the import account.

    Redmine keeps rows whose author was deleted; Specivo requires one.
    """
    author_id = await ctx.id_map.get(ctx.session, EntityType.USER, author_ref)
    if author_id is not None:
        from specivo.models.user import User

        author = await ctx.session.get(User, author_id)
        if author is not None:
            return author
    return await ensure_import_account(ctx)


# --------------------------------------------------------------------------
# Watchers
# --------------------------------------------------------------------------


async def load_watchers(ctx: PhaseContext) -> None:
    """Subscribe imported users to the issues they watched."""
    for project_ref in ctx.project_refs:
        async for ir in ctx.adapter.extract_watchers(project_ref):
            issue_id = await ctx.id_map.get(ctx.session, EntityType.ISSUE, ir.container_ref)
            user_id = await ctx.id_map.get(ctx.session, EntityType.USER, ir.user_ref)
            if issue_id is None or user_id is None:
                continue

            # The issue service auto-watches the author and assignee, so a
            # source watcher may already be subscribed.
            existing = await ctx.session.execute(
                select(Watcher.id).where(Watcher.issue_id == issue_id, Watcher.user_id == user_id).limit(1)
            )
            if existing.scalar_one_or_none() is not None:
                ctx.summary.record_skipped(EntityType.WATCHER)
                continue

            ctx.session.add(Watcher(issue_id=issue_id, user_id=user_id))
            ctx.summary.record_created(EntityType.WATCHER)
            ctx.tick()
    await ctx.session.flush()


# --------------------------------------------------------------------------
# Custom-field references
# --------------------------------------------------------------------------


async def resolve_custom_field_references(ctx: PhaseContext) -> None:
    """Rewrite user and version custom-field values to Specivo ids.

    These are stored as source ids while issues are created, because the
    referenced user or version may be imported after the issue that names it.
    """
    fields = ctx.state.get("custom_field_keys") or {}
    reference_fields = [field for field in fields.values() if field.field_format in {"user", "version"}]
    if not reference_fields:
        return

    for field in reference_fields:
        entity = EntityType.USER if field.field_format == "user" else EntityType.VERSION
        mapping = await ctx.id_map.preload(ctx.session, entity)
        if not mapping:
            continue
        await _rewrite_metadata_key(ctx.session, field.key, mapping)
        ctx.tick()


async def _rewrite_metadata_key(session: AsyncSession, key: str, mapping: dict[str, int]) -> None:
    """Replace source ids stored under *key* with the Specivo ids they map to."""
    stmt = select(Issue.id, Issue.issue_metadata).where(Issue.issue_metadata.has_key(key))
    rows = (await session.execute(stmt)).all()

    for issue_id, metadata in rows:
        current = metadata.get(key)
        if isinstance(current, list):
            resolved: Any = [mapping.get(str(item), item) for item in current]
        else:
            resolved = mapping.get(str(current), current)
        if resolved == current:
            continue
        updated = dict(metadata)
        updated[key] = resolved
        await session.execute(
            update(Issue)
            .where(Issue.id == issue_id)
            .values(issue_metadata=updated)
            .execution_options(synchronize_session=False)
        )


# --------------------------------------------------------------------------
# Journals
# --------------------------------------------------------------------------


async def load_journals(ctx: PhaseContext) -> None:
    """Replay issue history: comments and field changes, in order."""
    await ensure_import_account(ctx)

    for project_ref in ctx.project_refs:
        sequences: dict[int, int] = {}
        async for ir in ctx.adapter.extract_journals(project_ref):
            if await ctx.id_map.get(ctx.session, EntityType.JOURNAL, ir.source_ref):
                ctx.summary.record_skipped(EntityType.JOURNAL)
                continue
            await _create_journal(ctx, ir, sequences)


async def _create_journal(ctx: PhaseContext, ir: IRJournalEntry, sequences: dict[int, int]) -> None:
    """Create one journal entry with its field changes."""
    issue_id = await ctx.id_map.get(ctx.session, EntityType.ISSUE, ir.issue_ref)
    if issue_id is None:
        return

    issue = await ctx.session.get(Issue, issue_id)
    if issue is None:
        return

    author = await _resolve_author(ctx, ir.user_ref)

    if issue_id not in sequences:
        sequences[issue_id] = await _highest_sequence(ctx.session, issue_id)
    sequences[issue_id] += 1

    journal = Journal(
        issue_id=issue_id,
        project_id=issue.project_id,
        user_id=author.id,
        notes=convert_markup(ctx, ir.notes),
        is_private=ir.is_private,
        sequence=sequences[issue_id],
    )
    ctx.session.add(journal)
    await ctx.session.flush()

    for detail in ir.details:
        ctx.session.add(
            JournalDetail(
                journal_id=journal.id,
                property=detail.property[:30],
                prop_key=detail.prop_key[:255],
                old_value=detail.old_value,
                new_value=detail.new_value,
            )
        )

    await ctx.session.flush()
    await backdate(ctx.session, Journal, journal.id, created_at=ir.created_at, updated_at=ir.created_at)
    await ctx.id_map.put(ctx.session, EntityType.JOURNAL, ir.source_ref, "journals", journal.id)
    ctx.summary.record_created(EntityType.JOURNAL)
    ctx.tick()


async def _highest_sequence(session: AsyncSession, issue_id: int) -> int:
    """Return the highest journal sequence an issue already has.

    Creating an issue with a description leaves a baseline journal behind, so
    imported history continues from there rather than colliding with it.
    """
    stmt = select(func.coalesce(func.max(Journal.sequence), 0)).where(Journal.issue_id == issue_id)
    return (await session.execute(stmt)).scalar_one()


# --------------------------------------------------------------------------
# Relations
# --------------------------------------------------------------------------


async def load_relations(ctx: PhaseContext) -> None:
    """Link issues, once every issue in the run exists.

    Relations run late because one can cross projects, and the issue at the
    other end may belong to a project imported after this one.
    """
    for project_ref in ctx.project_refs:
        async for ir in ctx.adapter.extract_relations(project_ref):
            if await ctx.id_map.get(ctx.session, EntityType.RELATION, ir.source_ref):
                ctx.summary.record_skipped(EntityType.RELATION)
                continue
            await _create_relation(ctx, ir)


async def _create_relation(ctx: PhaseContext, ir: IRRelation) -> None:
    """Create one relation, letting the service normalise and validate it."""
    from_id = await ctx.id_map.get(ctx.session, EntityType.ISSUE, ir.from_ref)
    to_id = await ctx.id_map.get(ctx.session, EntityType.ISSUE, ir.to_ref)
    if from_id is None or to_id is None:
        ctx.warn("Relation points at an issue that was not imported; skipped", relation=ir.source_ref)
        return

    issue_from = await ctx.session.get(Issue, from_id)
    issue_to = await ctx.session.get(Issue, to_id)
    if issue_from is None or issue_to is None:
        return

    try:
        relation = await _relation_service.create(ctx.session, issue_from, issue_to, ir.relation_type, delay=ir.delay)
    except AppError as exc:
        # Specivo validates more strictly than Redmine — it refuses a relation
        # between an issue and its own descendant, for one. A relation the
        # source allowed is reported rather than ending the import.
        ctx.warn(
            "Relation was rejected and skipped",
            relation=ir.source_ref,
            type=ir.relation_type,
            reason=str(exc),
        )
        return

    await ctx.id_map.put(ctx.session, EntityType.RELATION, ir.source_ref, "issue_relations", relation.id)
    ctx.summary.record_created(EntityType.RELATION)
    ctx.tick()


async def _project_for(ctx: PhaseContext, project_ref: str) -> Project | None:
    project_id = await ctx.id_map.get(ctx.session, EntityType.PROJECT, project_ref)
    if project_id is None:
        return None
    return await ctx.session.get(Project, project_id)
