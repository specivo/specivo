"""Expand reference placeholders in imported text.

A source that names records by placeholder (see
:mod:`specivo.importers.core.placeholders`) leaves them in descriptions,
comments and wiki text while those are created, because the record a
placeholder names may not exist yet: an issue can link to a wiki page, and wiki
pages are imported after issues. This pass runs once everything is in and
replaces each placeholder with the Specivo form.

Only rows this source imported are touched, found through the id map, so text
somebody wrote in Specivo is never rewritten. A placeholder is looked up in the
id map of the run's scope (its source system and instance), which holds every
earlier run's records too, so a bundle may name an issue a previous bundle of
the same source brought in. A placeholder whose record was not imported, in
this run or an earlier one, is written as plain text, its label or else its
ref, so a reader never sees the placeholder syntax; they are counted in one
summary warning.

Each text left with such a placeholder is returned as a
:class:`~specivo.importers.core.links_file.PendingText`, so the import can keep
it in its links file. :func:`resolve_pending` is the other half: once a later
run has brought the records, it expands those texts again from their source,
unless they were edited since (see :mod:`specivo.importers.load.pending_links`).

Each text is expanded in a single pass: what an expansion produces is never
read as a placeholder in turn.

Sources that write Redmine-style ``#123`` references instead are handled by
:mod:`specivo.importers.load.reference_rewrite`; an adapter says which style it
uses with ``reference_style``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Select, Update, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.importers.core.id_map import ImportIdMap
from specivo.importers.core.ir import EntityType
from specivo.importers.core.links_file import LinksStore, PendingText, text_sha256
from specivo.importers.core.pipeline import PhaseContext
from specivo.importers.core.placeholders import (
    PLACEHOLDER_ENTITY_TYPES,
    REFERENCE_STYLE_HASH,
    REFERENCE_STYLE_PLACEHOLDERS,
    Placeholder,
    PlaceholderKind,
    expand_placeholders,
    find_placeholders,
)
from specivo.models.attachment import Attachment
from specivo.models.issue import Issue
from specivo.models.journal import Journal, JournalDetail
from specivo.models.project import Project
from specivo.models.user import User
from specivo.models.wiki import Wiki, WikiContent, WikiPage

logger = logging.getLogger(__name__)

# Rows read per query when scanning imported text.
_BATCH = 500
# Placeholders named in the warning about those written as plain text.
_MAX_EXAMPLES = 10


def reference_style(ctx: PhaseContext) -> str:
    """Return how the run's source writes references between records.

    Adapters that predate placeholders write ``#123`` and declare nothing.
    """
    return str(getattr(ctx.adapter, "reference_style", REFERENCE_STYLE_HASH))


@dataclass(frozen=True, slots=True)
class _Target:
    """What a placeholder resolved to, enough to format it in any project."""

    text: str
    project_id: int | None = None
    project_key: str | None = None
    slug: str | None = None


@dataclass(frozen=True, slots=True)
class _TextColumn:
    """A column placeholder expansion writes, named as the links file names it."""

    entity_type: str
    column: str

    def rows(self, ids: list[int]) -> Select[int, str | None, int]:
        """Select ``(id, text, project_id)`` for the rows *ids*."""
        if self.entity_type == "issue":
            return select(Issue.id, Issue.description, Issue.project_id).where(Issue.id.in_(ids))
        if self.entity_type == "journal":
            return select(Journal.id, Journal.notes, Journal.project_id).where(Journal.id.in_(ids))
        if self.entity_type == "journal_detail":
            return (
                select(JournalDetail.id, getattr(JournalDetail, self.column), Journal.project_id)
                .join(Journal, Journal.id == JournalDetail.journal_id)
                .where(JournalDetail.id.in_(ids))
            )
        return (
            select(WikiContent.id, WikiContent.text, Wiki.project_id)
            .join(WikiPage, WikiPage.id == WikiContent.page_id)
            .join(Wiki, Wiki.id == WikiPage.wiki_id)
            .where(WikiContent.id.in_(ids))
        )

    def write(self, row_id: int, text: str) -> Update:
        """Set the column of row *row_id* to *text*, keeping ``updated_at``.

        ``updated_at`` is set to itself: expanding a reference is not an edit,
        and the column's onupdate would otherwise stamp the import time.
        """
        model: Any = {"issue": Issue, "journal": Journal, "journal_detail": JournalDetail}.get(
            self.entity_type, WikiContent
        )
        values: dict[str, Any] = {self.column: text}
        if model is not JournalDetail:
            values["updated_at"] = model.updated_at
        return update(model).where(model.id == row_id).values(values).execution_options(synchronize_session=False)


ISSUE_DESCRIPTION = _TextColumn("issue", "description")
JOURNAL_NOTES = _TextColumn("journal", "notes")
DETAIL_OLD_VALUE = _TextColumn("journal_detail", "old_value")
DETAIL_NEW_VALUE = _TextColumn("journal_detail", "new_value")
WIKI_TEXT = _TextColumn("wiki_version", "text")

# Every text expansion writes, by ``(entity_type, column)``.
TEXT_COLUMNS: dict[tuple[str, str], _TextColumn] = {
    (c.entity_type, c.column): c
    for c in (ISSUE_DESCRIPTION, JOURNAL_NOTES, DETAIL_OLD_VALUE, DETAIL_NEW_VALUE, WIKI_TEXT)
}


def _pending(column: _TextColumn, row_id: int, source: str, written: str, left: list[Placeholder]) -> PendingText:
    refs = tuple(sorted({(str(p.kind), p.ref) for p in left}))
    return PendingText(column.entity_type, row_id, column.column, source, text_sha256(written), refs)


async def rewrite_placeholders(ctx: PhaseContext, resolver: PlaceholderResolver | None = None) -> list[PendingText]:
    """Expand placeholders in issue descriptions, comments, description history and wiki text.

    Returns the texts left with placeholders whose records were not imported.
    """
    if reference_style(ctx) != REFERENCE_STYLE_PLACEHOLDERS:
        return []

    resolver = resolver or PlaceholderResolver(ctx.session, ctx.id_map)
    pending: list[PendingText] = []
    changed = 0
    changed += await _rewrite_column(ctx, resolver, EntityType.ISSUE, ISSUE_DESCRIPTION, pending)
    changed += await _rewrite_column(ctx, resolver, EntityType.JOURNAL, JOURNAL_NOTES, pending)
    changed += await _rewrite_description_changes(ctx, resolver, pending)
    changed += await _rewrite_column(ctx, resolver, EntityType.WIKI_VERSION, WIKI_TEXT, pending)

    unresolved = resolver.unresolved()
    if unresolved:
        # One warning however many there are: a source exported in parts can
        # leave thousands, and the summary should stay readable.
        ctx.warn(
            "Placeholders written as plain text (their label, or else their ref): they name records "
            "that were not imported, in this run or an earlier one of the same source",
            count=len(unresolved),
            examples=", ".join(f"{p.kind}:{p.ref}" for p in unresolved[:_MAX_EXAMPLES]),
        )
    if changed:
        logger.info("Expanded reference placeholders in %d records", changed)
    ctx.tick(changed)
    return pending


async def _rewrite_column(
    ctx: PhaseContext,
    resolver: PlaceholderResolver,
    entity_type: EntityType,
    column: _TextColumn,
    pending: list[PendingText],
) -> int:
    """Rewrite one text column of the rows imported as *entity_type*."""
    ids = sorted((await ctx.id_map.preload(ctx.session, entity_type)).values())
    changed = 0
    for start in range(0, len(ids), _BATCH):
        stmt = column.rows(ids[start : start + _BATCH])
        rows = (await ctx.session.execute(stmt.where(stmt.selected_columns[1].contains("{{")))).all()
        for row_id, text, project_id in rows:
            if text is None:
                continue
            updated, left = await resolver.expand(text, project_id)
            if updated == text:
                continue
            await ctx.session.execute(column.write(row_id, updated))
            if left:
                pending.append(_pending(column, row_id, text, updated, left))
            changed += 1
    return changed


async def _rewrite_description_changes(
    ctx: PhaseContext, resolver: PlaceholderResolver, pending: list[PendingText]
) -> int:
    """Rewrite the descriptions recorded in imported issues' history.

    That includes the one creating an issue records, which holds the
    description as it was loaded, placeholders and all.
    """
    issue_ids = sorted((await ctx.id_map.preload(ctx.session, EntityType.ISSUE)).values())
    changed = 0
    for start in range(0, len(issue_ids), _BATCH):
        rows = (
            await ctx.session.execute(
                select(JournalDetail.id, JournalDetail.old_value, JournalDetail.new_value, Journal.project_id)
                .join(Journal, Journal.id == JournalDetail.journal_id)
                .where(
                    Journal.issue_id.in_(issue_ids[start : start + _BATCH]),
                    JournalDetail.property == "attr",
                    JournalDetail.prop_key == "description",
                    or_(JournalDetail.old_value.contains("{{"), JournalDetail.new_value.contains("{{")),
                )
            )
        ).all()
        for detail_id, old_value, new_value, project_id in rows:
            row_changed = False
            for column, value in ((DETAIL_OLD_VALUE, old_value), (DETAIL_NEW_VALUE, new_value)):
                if not value:
                    continue
                updated, left = await resolver.expand(value, project_id)
                if updated == value:
                    continue
                await ctx.session.execute(column.write(detail_id, updated))
                if left:
                    pending.append(_pending(column, detail_id, value, updated, left))
                row_changed = True
            changed += row_changed
    return changed


@dataclass(slots=True)
class ResolveResult:
    """What :func:`resolve_pending` did to the texts waiting in a links file."""

    linked: list[tuple[str, int]] = field(default_factory=list)
    edited: list[tuple[str, int]] = field(default_factory=list)
    gone: int = 0
    waiting: int = 0


async def resolve_pending(
    session: AsyncSession,
    id_map: ImportIdMap,
    store: LinksStore,
    resolver: PlaceholderResolver | None = None,
) -> ResolveResult:
    """Link the waiting texts whose records the identifier map now holds.

    A text whose column still holds what Specivo wrote (the hashes match) is
    expanded again from its stored source text, against the current map, and
    written keeping ``updated_at``; it stays in *store* with whatever is still
    unresolved, or leaves it when nothing is. A text somebody edited since is
    left as it is and dropped from *store*; so is one whose row is gone. Writes
    go to *session* and *store*, neither of which is committed here.
    """
    resolver = resolver or PlaceholderResolver(session, id_map)
    result = ResolveResult()
    by_kind: dict[str, list[str]] = {}
    for kind, ref in store.refs():
        by_kind.setdefault(kind, []).append(ref)

    resolvable: list[tuple[str, str]] = []
    for kind, refs in by_kind.items():
        try:
            entity_type = PLACEHOLDER_ENTITY_TYPES[PlaceholderKind(kind)]
        except ValueError:
            continue
        found = await id_map.get_many(session, entity_type, refs)
        resolvable.extend((kind, ref) for ref in refs if ref in found)

    grouped: dict[tuple[str, str], list[PendingText]] = {}
    for text in store.texts_naming(resolvable) if resolvable else []:
        grouped.setdefault((text.entity_type, text.column), []).append(text)
    for key, group in grouped.items():
        column = TEXT_COLUMNS.get(key)
        if column is None:
            # Not a column this version writes: nothing can be done with it.
            for text in group:
                store.delete(*text.key)
                result.gone += 1
            continue
        for start in range(0, len(group), _BATCH):
            await _resolve_batch(session, resolver, store, column, group[start : start + _BATCH], result)

    result.waiting = store.count()
    return result


async def _resolve_batch(
    session: AsyncSession,
    resolver: PlaceholderResolver,
    store: LinksStore,
    column: _TextColumn,
    texts: list[PendingText],
    result: ResolveResult,
) -> None:
    current = {
        row_id: (text, project_id)
        for row_id, text, project_id in (await session.execute(column.rows([t.row_id for t in texts]))).all()
    }
    for pending in texts:
        text, project_id = current.get(pending.row_id, (None, None))
        if text is None:
            store.delete(*pending.key)
            result.gone += 1
            continue
        if text_sha256(text) != pending.written_sha256:
            # Edited after the import wrote it: somebody's work is never overwritten.
            store.delete(*pending.key)
            result.edited.append((pending.entity_type, pending.row_id))
            continue
        updated, left = await resolver.expand(pending.source_text, project_id)
        if updated != text:
            await session.execute(column.write(pending.row_id, updated))
            result.linked.append((pending.entity_type, pending.row_id))
        if left:
            store.put(_pending(column, pending.row_id, pending.source_text, updated, left))
        else:
            store.delete(*pending.key)


class PlaceholderResolver:
    """Resolves placeholders against the id map, caching each record it reads."""

    def __init__(self, session: AsyncSession, id_map: ImportIdMap) -> None:
        self._session = session
        self._id_map = id_map
        self._targets: dict[tuple[PlaceholderKind, str], _Target | None] = {}
        self._unresolved: dict[tuple[PlaceholderKind, str], Placeholder] = {}

    def unresolved(self) -> list[Placeholder]:
        """Return each placeholder that named a record not in the import, once."""
        return list(self._unresolved.values())

    async def expand(self, text: str, project_id: int | None) -> tuple[str, list[Placeholder]]:
        """Return *text* expanded for a row in *project_id*, and the placeholders left unresolved."""
        for placeholder in find_placeholders(text):
            key = (placeholder.kind, placeholder.ref)
            if key not in self._targets:
                self._targets[key] = await self._fetch(self._session, placeholder)

        def resolve(placeholder: Placeholder) -> str | None:
            target = self._targets.get((placeholder.kind, placeholder.ref))
            if target is None:
                self._unresolved.setdefault((placeholder.kind, placeholder.ref), placeholder)
                return None
            return _format(placeholder, target, project_id)

        return expand_placeholders(text, resolve)

    async def _fetch(self, session: AsyncSession, placeholder: Placeholder) -> _Target | None:
        target_id = await self._id_map.get(session, placeholder.entity_type, placeholder.ref)
        if target_id is None:
            return None

        if placeholder.kind is PlaceholderKind.ISSUE:
            issue = (
                await session.execute(select(Issue.project_key, Issue.sequence_number).where(Issue.id == target_id))
            ).one_or_none()
            return _Target(text=f"{issue[0]}-{issue[1]}") if issue else None

        if placeholder.kind is PlaceholderKind.USER:
            login = (await session.execute(select(User.login).where(User.id == target_id))).scalar_one_or_none()
            return _Target(text=f"@{login}") if login else None

        if placeholder.kind is PlaceholderKind.ATTACHMENT:
            filename = (
                await session.execute(select(Attachment.filename).where(Attachment.id == target_id))
            ).scalar_one_or_none()
            return _Target(text=link_target(filename)) if filename else None

        page = (
            await session.execute(
                select(WikiPage.title, WikiPage.slug, Wiki.project_id, Project.key)
                .join(Wiki, Wiki.id == WikiPage.wiki_id)
                .join(Project, Project.id == Wiki.project_id)
                .where(WikiPage.id == target_id)
            )
        ).one_or_none()
        if page is None:
            return None
        title, slug, page_project_id, project_key = page
        return _Target(text=title, project_id=page_project_id, project_key=project_key, slug=slug)


def _format(placeholder: Placeholder, target: _Target, project_id: int | None) -> str:
    """Return the Specivo text for *placeholder* in a row of *project_id*."""
    label = placeholder.label
    if placeholder.kind is PlaceholderKind.ISSUE:
        return f"[{_escape_label(label)}](/issue/{target.text}/)" if label else target.text
    if placeholder.kind is not PlaceholderKind.WIKI:
        return target.text

    title = target.text
    # [[Title]] links within the page's own project only, and cannot hold a
    # bracket, bar or backslash in its title or label (the renderer does not
    # escape them); anything else is an ordinary link, whose label is escaped
    # so it cannot close early and smuggle in a target.
    plain = not any(char in title + (label or "") for char in "[]|\\")
    if target.project_id == project_id and plain:
        return f"[[{title}|{label}]]" if label else f"[[{title}]]"
    return f"[{_escape_label(label or title)}](/projects/{target.project_key}/wiki/{target.slug}/)"


def link_target(filename: str) -> str:
    """Return *filename* as a Markdown link target.

    A name with a space or bracket in it is wrapped in ``<...>``, CommonMark's
    form for such targets, which the renderer resolves like a bare one.
    """
    if any(char in filename for char in " ()<>"):
        return f"<{filename.replace('<', '%3C').replace('>', '%3E')}>"
    return filename


def _escape_label(label: str) -> str:
    """Escape what would end a Markdown link label early.

    Backslashes are escaped first, so one at the end of a label cannot
    cancel the escape of the bracket after it.
    """
    return label.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
