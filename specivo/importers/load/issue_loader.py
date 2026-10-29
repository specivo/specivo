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
* A parent this run did not create before the child (one later in load order,
  or one of another bundle of the source) is set in the relations phase, once
  every issue of the run exists (see :mod:`~specivo.importers.load.issue_links`).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import AppError
from specivo.importers.core.backdate import backdate
from specivo.importers.core.converter import ContentConverter, ConversionContext
from specivo.importers.core.ir import (
    REFERENCE_ATTRIBUTES,
    REFERENCE_FIELD_FORMATS,
    EntityType,
    IRCustomField,
    IRIssue,
    IRJournalDetail,
    IRJournalEntry,
    ValueKind,
)
from specivo.importers.core.pipeline import PhaseContext
from specivo.importers.load.user_loader import ensure_import_account
from specivo.models.issue import Issue
from specivo.models.journal import ATTACHMENT_DETAIL, CF_USER, CF_VERSION, Journal, JournalDetail
from specivo.models.project import Project
from specivo.models.sprint import Sprint
from specivo.models.watcher import Watcher
from specivo.schemas.issue import IssueCreate
from specivo.services.issue_service import IssueService
from specivo.services.tag_service import TagService

logger = logging.getLogger(__name__)

# Where the converter is parked so every phase converts markup the same way.
CONVERTER_STATE_KEY = "converter"
CONVERSION_CONTEXT_STATE_KEY = "conversion_context"
# The issues this run created, by source ref: a child is created under one of these at once.
CREATED_ISSUES_STATE_KEY = "issues_created"
# Unresolved history refs named in the warning about them.
_MAX_EXAMPLES = 10
# What a custom field change whose values name a record is stored as.
_CF_REFERENCE_PROPERTIES = {EntityType.USER: CF_USER, EntityType.VERSION: CF_VERSION}

_issue_service = IssueService()
_tag_service = TagService()


def converter_for(ctx: PhaseContext) -> tuple[ContentConverter, ConversionContext]:
    """Return the markup converter for this run, building it once."""
    converter = ctx.state.get(CONVERTER_STATE_KEY)
    if converter is None:
        converter = ctx.adapter.content_converter()
        ctx.state[CONVERTER_STATE_KEY] = converter
        ctx.state[CONVERSION_CONTEXT_STATE_KEY] = ConversionContext()
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

        # Creating a child recalculates its parent's progress and dates, which
        # stamps the parent's updated_at with the time of the import. That is
        # derived data, not an edit, so each parent gets the source's time back
        # once the project's issues are in.
        updated_at: dict[str, datetime | None] = {}
        parents: set[str] = set()
        async for ir in ctx.adapter.extract_issues(project_ref):
            updated_at[ir.source_ref] = ir.updated_at
            if await ctx.id_map.get(ctx.session, EntityType.ISSUE, ir.source_ref):
                ctx.summary.record_skipped(EntityType.ISSUE)
                continue
            issue = await _create_issue(ctx, project, ir)
            if issue is not None and issue.parent_id is not None and ir.parent_ref is not None:
                parents.add(ir.parent_ref)
        await _restore_parent_timestamps(ctx, parents, updated_at)


async def _restore_parent_timestamps(
    ctx: PhaseContext, parent_refs: set[str], updated_at: dict[str, datetime | None]
) -> None:
    """Put back the source's updated_at on parents a new child just touched."""
    for parent_ref in sorted(parent_refs):
        parent_id = await ctx.id_map.get(ctx.session, EntityType.ISSUE, parent_ref)
        if parent_id is not None and updated_at.get(parent_ref) is not None:
            await backdate(ctx.session, Issue, parent_id, updated_at=updated_at[parent_ref])


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
        # A source can assign an issue to a group, which Specivo cannot express.
        ctx.warn("Issue is assigned to someone who was not imported; left unassigned", issue=ir.source_ref)

    # A parent this run created is set now, as the issue is created. Any other
    # (later in load order, or from another bundle of the source) is set in the
    # relations phase, or kept waiting in the links file.
    created: set[str] = ctx.state.setdefault(CREATED_ISSUES_STATE_KEY, set())
    parent_id = None
    if ir.parent_ref is not None and ir.parent_ref in created:
        parent_id = await ctx.id_map.get(ctx.session, EntityType.ISSUE, ir.parent_ref)

    metadata = await _metadata(ctx, ir)

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
            # The source's own privacy flag is history, not a new choice: it
            # is kept as it was, whatever the tracker's default says.
            check_privacy_permission=False,
            limit_text_length=False,
        )
    except AppError as exc:
        ctx.warn("Issue could not be created; skipped", issue=ir.source_ref, reason=str(exc))
        return None

    if ir.remaining_estimate is not None:
        # Not part of IssueCreate: nothing in Specivo edits it, so it is kept
        # as the source left it. Flushed before the timestamps are restored.
        issue.remaining_estimate = ir.remaining_estimate
        await ctx.session.flush()
    await _set_sprint(ctx, issue, ir)
    await _set_tags(ctx, project, issue, ir)

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
            .values(created_at=ir.created_at, updated_at=ir.created_at)
            .execution_options(synchronize_session=False)
        )

    await ctx.id_map.put(ctx.session, EntityType.ISSUE, ir.source_ref, "issues", issue.id)
    created.add(ir.source_ref)
    ctx.summary.record_created(EntityType.ISSUE)
    ctx.tick()
    return issue


async def _set_sprint(ctx: PhaseContext, issue: Issue, ir: IRIssue) -> None:
    """Put the issue in its sprint, set directly rather than through ``IssueCreate``.

    The service accepts only a planned or active sprint, as anyone filing an
    issue today would pick; an imported issue may sit in a completed one.
    The sprint must be one of the issue's own project, as the service requires.
    """
    if ir.sprint_ref is None:
        return
    sprint_id = await ctx.id_map.get(ctx.session, EntityType.SPRINT, ir.sprint_ref)
    sprint = await ctx.session.get(Sprint, sprint_id) if sprint_id is not None else None
    if sprint is None or sprint.project_id != issue.project_id:
        ctx.warn("Issue's sprint was not imported in its project; left out of a sprint", issue=ir.source_ref)
        return
    issue.sprint_id = sprint.id
    await ctx.session.flush()


async def _set_tags(ctx: PhaseContext, project: Project, issue: Issue, ir: IRIssue) -> None:
    """Tag the issue, creating each tag in its project the first time it is named.

    Tag names are unique per project ignoring case, so names differing only in
    case are one tag, spelled as first named.
    """
    if not ir.tags:
        return
    account = await ensure_import_account(ctx)
    for name in dict.fromkeys(tag.strip() for tag in ir.tags if tag.strip()):
        await _tag_service.add_to_issue(ctx.session, project, issue.id, name[:64], account)


async def _metadata(ctx: PhaseContext, ir: IRIssue) -> dict[str, Any]:
    """Return the issue's custom-field values, keyed as its metadata stores them.

    A bundle names users and versions by ref strings that a schema expecting a
    Specivo id would reject, so they are resolved here: users and versions are
    imported before issues. A value naming something that was not imported is
    left out.
    """
    metadata: dict[str, Any] = {}
    for value in ir.custom_values:
        if value.value_kind is ValueKind.SCALAR:
            metadata[value.key] = value.value
            continue
        entity = EntityType.USER if value.value_kind is ValueKind.USER_REF else EntityType.VERSION
        refs = value.value if isinstance(value.value, list) else [value.value]
        resolved = [await ctx.id_map.get(ctx.session, entity, str(ref)) for ref in refs]
        if None in resolved:
            ctx.warn(
                f"Custom field value names a {entity} that was not imported; left out",
                issue=ir.source_ref,
                field=value.key,
            )
        found = [target for target in resolved if target is not None]
        if isinstance(value.value, list):
            metadata[value.key] = found
        elif found:
            metadata[value.key] = found[0]
    return metadata


async def _resolve_author(ctx: PhaseContext, author_ref: str | None):
    """Return the issue's author, falling back to the import account.

    A source can keep rows whose author was deleted; Specivo requires one.
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
# Journals
# --------------------------------------------------------------------------


async def load_journals(ctx: PhaseContext) -> None:
    """Replay issue history: comments and field changes, in order."""
    await ensure_import_account(ctx)

    unresolved: list[str] = []
    fields = {field.source_ref: field async for field in ctx.adapter.extract_custom_fields()}
    for project_ref in ctx.project_refs:
        sequences: dict[int, int] = {}
        async for ir in ctx.adapter.extract_journals(project_ref):
            if await ctx.id_map.get(ctx.session, EntityType.JOURNAL, ir.source_ref):
                ctx.summary.record_skipped(EntityType.JOURNAL)
                continue
            await _create_journal(ctx, ir, sequences, unresolved, fields)
    if unresolved:
        # One warning however many: history routinely names deleted versions and the like.
        ctx.warn(
            "History values name records that were not imported; their text from the source is shown "
            "instead, or nothing when the source gave none",
            count=len(unresolved),
            examples=", ".join(list(dict.fromkeys(unresolved))[:_MAX_EXAMPLES]),
        )


async def _create_journal(
    ctx: PhaseContext,
    ir: IRJournalEntry,
    sequences: dict[int, int],
    unresolved: list[str],
    fields: dict[str, IRCustomField],
) -> None:
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
        prop, prop_key, entity = _detail_kind(detail, fields)
        old_value, new_value = detail.old_value, detail.new_value
        if entity is not None:
            old_value = await _reference_value(ctx, entity, detail.old_ref, old_value, unresolved)
            new_value = await _reference_value(ctx, entity, detail.new_ref, new_value, unresolved)
        elif prop == ATTACHMENT_DETAIL:
            prop_key = await _attachment_key(ctx, prop_key, unresolved)
        ctx.session.add(
            JournalDetail(
                journal_id=journal.id,
                property=prop[:30],
                prop_key=prop_key[:255],
                old_value=old_value,
                new_value=new_value,
            )
        )

    await ctx.session.flush()
    await backdate(ctx.session, Journal, journal.id, created_at=ir.created_at, updated_at=ir.created_at)
    await ctx.id_map.put(ctx.session, EntityType.JOURNAL, ir.source_ref, "journals", journal.id)
    ctx.summary.record_created(EntityType.JOURNAL)
    ctx.tick()


def _detail_kind(detail: IRJournalDetail, fields: dict[str, IRCustomField]) -> tuple[str, str, EntityType | None]:
    """Return a detail's ``property`` and ``prop_key`` as Specivo stores them, and what its values name.

    An attribute is stored as the bundle names it. A custom field change is
    stored under the field's metadata key, the name its values have on the
    issue, and a field whose values name a person or a version as
    :data:`~specivo.models.journal.CF_USER` / ``CF_VERSION``, so history can
    name them. A field the bundle does not define keeps its ref, and its
    values are text.
    """
    if detail.property == "attr":
        return detail.property, detail.prop_key, REFERENCE_ATTRIBUTES.get(detail.prop_key)
    field = fields.get(detail.prop_key) if detail.property == "cf" else None
    if field is None:
        return detail.property, detail.prop_key, None
    entity = REFERENCE_FIELD_FORMATS.get(field.field_format)
    if entity is None:
        return detail.property, field.key, None
    return _CF_REFERENCE_PROPERTIES[entity], field.key, entity


async def _reference_value(
    ctx: PhaseContext, entity: EntityType, ref: str | None, text: str | None, unresolved: list[str]
) -> str | None:
    """Return what a history value naming a record is stored as.

    Specivo's history keeps the Specivo id of the record, as the issue page
    looks names up by it; a parent is kept as its display key, as relation
    changes are. A ref naming nothing this source imported, in this run or an
    earlier one, is never stored as it is: it would be read as the id of an
    unrelated Specivo record. The source's text for the value is stored
    instead, quoted when it is only digits for the same reason, or nothing.
    """
    if ref is not None:
        target_id = await ctx.id_map.get(ctx.session, entity, ref)
        if target_id is not None and entity is EntityType.ISSUE:
            issue = await ctx.session.get(Issue, target_id)
            if issue is not None:
                return issue.display_key
        elif target_id is not None:
            return str(target_id)
        unresolved.append(f"{entity}:{ref}")
    if text is None or not text.strip():
        return None
    return f'"{text}"' if text.isdigit() else text


async def _attachment_key(ctx: PhaseContext, ref: str, unresolved: list[str]) -> str:
    """Return what an attachment change's ``prop_key`` (the file's ``source_ref``) is stored as.

    Specivo's history keeps the attachment's id, by which the issue page names
    the file as it was imported (renamed, say, when the issue holds two files
    of one name). A ref naming no imported file (history names files deleted
    since) is never stored as it is, as it would be read as the id of an
    unrelated Specivo file: the key is left empty and the file named by the
    detail's value, the source's file name.
    """
    target_id = await ctx.id_map.get(ctx.session, EntityType.ATTACHMENT, ref) if ref else None
    if target_id is not None:
        return str(target_id)
    if ref:
        unresolved.append(f"{EntityType.ATTACHMENT}:{ref}")
    return ""


async def _highest_sequence(session: AsyncSession, issue_id: int) -> int:
    """Return the highest journal sequence an issue already has.

    Creating an issue with a description leaves a baseline journal behind, so
    imported history continues from there rather than colliding with it.
    """
    stmt = select(func.coalesce(func.max(Journal.sequence), 0)).where(Journal.issue_id == issue_id)
    return (await session.execute(stmt)).scalar_one()


async def _project_for(ctx: PhaseContext, project_ref: str) -> Project | None:
    project_id = await ctx.id_map.get(ctx.session, EntityType.PROJECT, project_ref)
    if project_id is None:
        return None
    return await ctx.session.get(Project, project_id)
