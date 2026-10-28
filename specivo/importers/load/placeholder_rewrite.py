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
summary warning. A later run that imports the record does not link that text:
load the records a text refers to first.

Each text is expanded in a single pass: what an expansion produces is never
read as a placeholder in turn.

Sources that write Redmine-style ``#123`` references instead are handled by
:mod:`specivo.importers.load.reference_rewrite`; an adapter says which style it
uses with ``reference_style``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.importers.core.ir import EntityType
from specivo.importers.core.pipeline import PhaseContext
from specivo.importers.core.placeholders import (
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


async def rewrite_placeholders(ctx: PhaseContext) -> None:
    """Expand placeholders in issue descriptions, comments, description history and wiki text."""
    if reference_style(ctx) != REFERENCE_STYLE_PLACEHOLDERS:
        return

    resolver = _Resolver(ctx)
    changed = 0
    changed += await _rewrite_column(ctx, resolver, EntityType.ISSUE, Issue, Issue.description, Issue.project_id)
    changed += await _rewrite_column(ctx, resolver, EntityType.JOURNAL, Journal, Journal.notes, Journal.project_id)
    changed += await _rewrite_description_changes(ctx, resolver)
    changed += await _rewrite_wiki(ctx, resolver)

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


async def _rewrite_column(
    ctx: PhaseContext,
    resolver: _Resolver,
    entity_type: EntityType,
    model: Any,
    column: Any,
    project_column: Any,
) -> int:
    """Rewrite one text column of the rows imported as *entity_type*."""
    ids = sorted((await ctx.id_map.preload(ctx.session, entity_type)).values())
    changed = 0
    for start in range(0, len(ids), _BATCH):
        rows = (
            await ctx.session.execute(
                select(model.id, column, project_column).where(
                    model.id.in_(ids[start : start + _BATCH]), column.contains("{{")
                )
            )
        ).all()
        for row_id, text, project_id in rows:
            updated = await resolver.expand(text, project_id)
            if updated == text:
                continue
            await ctx.session.execute(
                update(model)
                .where(model.id == row_id)
                # Set to itself: expanding a reference is not an edit, and
                # the column's onupdate would otherwise stamp the import time.
                .values({column.key: updated, "updated_at": model.updated_at})
                .execution_options(synchronize_session=False)
            )
            changed += 1
    return changed


async def _rewrite_description_changes(ctx: PhaseContext, resolver: _Resolver) -> int:
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
            old_updated = await resolver.expand(old_value, project_id) if old_value else old_value
            new_updated = await resolver.expand(new_value, project_id) if new_value else new_value
            if (old_updated, new_updated) == (old_value, new_value):
                continue
            await ctx.session.execute(
                update(JournalDetail)
                .where(JournalDetail.id == detail_id)
                .values(old_value=old_updated, new_value=new_updated)
                .execution_options(synchronize_session=False)
            )
            changed += 1
    return changed


async def _rewrite_wiki(ctx: PhaseContext, resolver: _Resolver) -> int:
    """Rewrite every imported wiki revision, each in its own page's project."""
    ids = sorted((await ctx.id_map.preload(ctx.session, EntityType.WIKI_VERSION)).values())
    changed = 0
    for start in range(0, len(ids), _BATCH):
        rows = (
            await ctx.session.execute(
                select(WikiContent.id, WikiContent.text, Wiki.project_id)
                .join(WikiPage, WikiPage.id == WikiContent.page_id)
                .join(Wiki, Wiki.id == WikiPage.wiki_id)
                .where(WikiContent.id.in_(ids[start : start + _BATCH]), WikiContent.text.contains("{{"))
            )
        ).all()
        for content_id, text, project_id in rows:
            updated = await resolver.expand(text, project_id)
            if updated == text:
                continue
            await ctx.session.execute(
                update(WikiContent)
                .where(WikiContent.id == content_id)
                .values(text=updated, updated_at=WikiContent.updated_at)
                .execution_options(synchronize_session=False)
            )
            changed += 1
    return changed


class _Resolver:
    """Resolves placeholders against the id map, caching each record it reads."""

    def __init__(self, ctx: PhaseContext) -> None:
        self._ctx = ctx
        self._targets: dict[tuple[PlaceholderKind, str], _Target | None] = {}
        self._unresolved: dict[tuple[PlaceholderKind, str], Placeholder] = {}

    def unresolved(self) -> list[Placeholder]:
        """Return each placeholder that named a record not in the import, once."""
        return list(self._unresolved.values())

    async def expand(self, text: str, project_id: int | None) -> str:
        """Return *text* with its placeholders expanded for a row in *project_id*."""
        for placeholder in find_placeholders(text):
            key = (placeholder.kind, placeholder.ref)
            if key not in self._targets:
                self._targets[key] = await self._fetch(self._ctx.session, placeholder)

        def resolve(placeholder: Placeholder) -> str | None:
            target = self._targets.get((placeholder.kind, placeholder.ref))
            if target is None:
                self._unresolved.setdefault((placeholder.kind, placeholder.ref), placeholder)
                return None
            return _format(placeholder, target, project_id)

        expanded, _ = expand_placeholders(text, resolve)
        return expanded

    async def _fetch(self, session: AsyncSession, placeholder: Placeholder) -> _Target | None:
        target_id = await self._ctx.id_map.get(session, placeholder.entity_type, placeholder.ref)
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
