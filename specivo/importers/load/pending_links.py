"""Link text an earlier import wrote as plain text, through the links file.

A source exported in parts (one project at a time) can name records that come
with a later bundle. The placeholder rewrite writes those references as plain
text and returns the texts concerned; this module keeps them in the run's links
file (:mod:`specivo.importers.core.links_file`) and, on every later import of
the same source, re-expands the ones whose records have arrived, unless
somebody edited them in the meantime. Relations and parents waiting in the
same file are made by :mod:`specivo.importers.load.issue_links`, which opens
the file through :func:`open_for_phase` too.

The file sits next to the bundle by default
(``specivo-links.<system>.<instance>.sqlite``), or wherever ``--links-file``
puts it. The SQLite transaction is committed only after the database
transaction of the phase has committed, so the file never describes rows that
were rolled back. Nothing here can fail an import: a file that cannot be read
or written is reported and the import carries on without it; only a file that
belongs to another import is refused, before anything is written.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.importers.core.id_map import ImportIdMap
from specivo.importers.core.links_file import (
    LinksFileError,
    LinksFileMismatchError,
    LinksFileUnreadableError,
    LinksStore,
    check_scope,
    is_writable,
    read_scope,
)
from specivo.importers.core.pipeline import PhaseContext, PhaseFailedError
from specivo.importers.load.placeholder_rewrite import (
    PlaceholderResolver,
    ResolveResult,
    resolve_pending,
    rewrite_placeholders,
)
from specivo.models.wiki import WikiContent, WikiPage

logger = logging.getLogger(__name__)

# Set when the file cannot be read or written, so the rest of the run leaves it alone.
_UNREADABLE = "links_file_unreadable"
# Set once the run has said the file cannot be written, so it says so once.
_UNWRITABLE_REPORTED = "links_file_unwritable_reported"
# Rows named in a summary line about linked or edited texts.
_MAX_EXAMPLES = 5

LINKED_NOTE = "Text from earlier imports now linked"
WAITING_NOTE = "Text waiting for records a later import may bring"


def links_path(ctx: PhaseContext) -> Path | None:
    """Return the run's links file, or ``None`` for a source that keeps none."""
    path = getattr(ctx.adapter, "links_path", None)
    return Path(path) if path is not None else None


def usable(ctx: PhaseContext) -> bool:
    """Whether the run may use its links file: the bootstrap check did not find it unreadable."""
    return not ctx.state.get(_UNREADABLE)


def open_for_phase(ctx: PhaseContext, path: Path, *, has_new: bool) -> LinksStore | None:
    """Open the links file for the current phase, committed only once the phase's transaction has.

    *has_new* says the phase has rows to add, so a location that cannot be
    written is worth a warning even in a dry run. Returns ``None``, having
    said why, when the file cannot be used.
    """
    store = _open(ctx, path, has_new=has_new)
    if store is not None:
        ctx.at_phase_end(lambda committed: _finish(ctx, store, committed))
    return store


def check_links_file(ctx: PhaseContext) -> None:
    """Refuse, before anything is written, a links file that belongs to another import.

    A file that cannot be read is reported and left alone for the rest of the run.
    """
    path = links_path(ctx)
    if path is None or not os.path.lexists(path):
        return
    try:
        check_scope(path, ctx.id_map.source_system, ctx.id_map.source_instance)
    except LinksFileMismatchError as exc:
        raise PhaseFailedError(f"{exc}. Nothing was imported.") from exc
    except LinksFileUnreadableError as exc:
        ctx.state[_UNREADABLE] = True
        ctx.warn(
            "The links file could not be read, so references earlier imports wrote as plain text are not linked by "
            "this one, and this one's are not kept. Delete it, or give another path with --links-file",
            path=str(path),
            reason=str(exc),
        )


async def link_placeholders(ctx: PhaseContext) -> None:
    """Expand this run's placeholders, then link earlier imports' text through the links file."""
    resolver = PlaceholderResolver(ctx.session, ctx.id_map)
    pending = await rewrite_placeholders(ctx, resolver)
    path = links_path(ctx)
    if path is None or not usable(ctx):
        return
    if not os.path.lexists(path) and not pending:
        return

    store = open_for_phase(ctx, path, has_new=bool(pending))
    if store is None:
        return
    result = await resolve_pending(ctx.session, ctx.id_map, store, resolver)
    for text in pending:
        store.put(text)
    _report(ctx, result, store, path)


def _open(ctx: PhaseContext, path: Path, *, has_new: bool) -> LinksStore | None:
    """Open the links file for this run, or say why not and return ``None``."""
    writable = is_writable(path)
    if not writable and (has_new or not ctx.options.dry_run):
        _warn_unwritable(ctx, path)
        if not ctx.options.dry_run:
            ctx.state[_UNREADABLE] = True
            return None
    try:
        # A dry run works on a copy: it reports what would be linked and writes nothing.
        return LinksStore.open(
            path, ctx.id_map.source_system, ctx.id_map.source_instance, writable=not ctx.options.dry_run
        )
    except LinksFileMismatchError as exc:
        raise PhaseFailedError(f"{exc}. Nothing was imported.") from exc
    except LinksFileUnreadableError as exc:
        ctx.state[_UNREADABLE] = True
        ctx.warn(
            "The links file could not be read, so references earlier imports wrote as plain text are not linked by "
            "this one, and this one's are not kept. Delete it, or give another path with --links-file",
            path=str(path),
            reason=str(exc),
        )
    except (sqlite3.Error, OSError) as exc:
        ctx.state[_UNREADABLE] = True
        _warn_unwritable(ctx, path, reason=str(exc))
    return None


def _warn_unwritable(ctx: PhaseContext, path: Path, reason: str | None = None) -> None:
    if ctx.state.get(_UNWRITABLE_REPORTED):
        return
    ctx.state[_UNWRITABLE_REPORTED] = True
    context = {"path": str(path)}
    if reason:
        context["reason"] = reason
    ctx.warn(
        "The links file cannot be written, so references this import writes as plain text will not be turned "
        "into links by a later import, nor earlier ones' by this one. Give a writable path with --links-file",
        **context,
    )


def _examples(rows: list[tuple[str, int]]) -> str:
    return ", ".join(f"{entity} {row_id}" for entity, row_id in rows[:_MAX_EXAMPLES])


def _report(ctx: PhaseContext, result: ResolveResult, store: LinksStore, path: Path) -> None:
    if result.linked:
        ctx.summary.add_note(LINKED_NOTE, f"{len(result.linked)} text(s), e.g. {_examples(result.linked)}")
    if result.untrusted:
        ctx.warn(
            "Rows of the links file that this installation did not write were ignored and removed (no valid "
            "signature, or not a text an import writes): a links file only works with the Specivo, and the "
            "SECRET_KEY, that wrote it",
            count=result.untrusted,
            path=str(path),
        )
    if result.foreign:
        ctx.warn(
            "Rows of the links file naming records this source did not import were ignored and removed",
            count=result.foreign,
            path=str(path),
        )
    if result.edited:
        ctx.warn(
            "Texts edited since an earlier import were left as they are, so references in them whose records this "
            "run imported were not linked",
            count=len(result.edited),
            examples=_examples(result.edited),
        )
    waiting = store.count_texts()
    if waiting:
        ctx.summary.add_note(WAITING_NOTE, f"{waiting} text(s), kept in {path}")


def _finish(ctx: PhaseContext, store: LinksStore, committed: bool) -> None:
    """Commit the links file once the database has, or discard what this run did to it."""
    if not committed:
        store.rollback()
        return
    try:
        store.commit()
    except (sqlite3.Error, OSError) as exc:
        store.rollback()
        # The database work is committed and stays: report, never fail. Not
        # through ctx.warn, which a strict run turns into a failure.
        message = (
            "The links file could not be saved, so references written as plain text will not be turned into "
            "links by a later import"
        )
        context = {"path": str(store.path), "reason": str(exc)}
        ctx.reporter.warning(message, {"phase": str(ctx.phase), **context})
        ctx.summary.add_warning(str(ctx.phase), message, context)


# ---------------------------------------------------------------------------
# On demand: admin import-map resolve-pending
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class LinksOutcome:
    """What resolving a links file on demand did, or would do."""

    path: Path
    scope: tuple[str, str] | None
    linked: list[tuple[str, int]] = field(default_factory=list)
    edited: list[tuple[str, int]] = field(default_factory=list)
    gone: int = 0
    waiting: int = 0
    untrusted: int = 0
    foreign: int = 0
    relations_linked: int = 0
    parents_linked: int = 0
    links_waiting: int = 0
    # Relations and parents refused or left alone, one line each.
    link_problems: list[str] = field(default_factory=list)


async def resolve_links_file(session: AsyncSession, path: Path, *, dry_run: bool) -> LinksOutcome:
    """Run the resolve step against the links file at *path*, outside an import.

    Waiting parents are set first, then waiting relations made, then texts
    linked, as an import does. The scope comes from the file. A dry run does
    the work inside a savepoint it rolls back, and on a copy of the file.
    Otherwise the session is committed and then the file. Raises
    :class:`LinksFileError` for a file that cannot be used.
    """
    from specivo.importers.load.issue_links import LinkResult, resolve_waiting_links

    if not os.path.lexists(path):
        return LinksOutcome(path, None)
    scope = read_scope(path)
    if scope is None:
        return LinksOutcome(path, None)
    store = LinksStore.open(path, *scope, writable=not dry_run)
    try:
        id_map = ImportIdMap(scope[0], scope[1], uuid.uuid4())
        links = LinkResult()
        if dry_run:
            savepoint = await session.begin_nested()
            try:
                links = await resolve_waiting_links(session, id_map, store)
                result = await resolve_pending(session, id_map, store)
            finally:
                await savepoint.rollback()
            waiting_links = sum(store.waiting_links().values())
            store.rollback()
        else:
            links = await resolve_waiting_links(session, id_map, store)
            result = await resolve_pending(session, id_map, store)
            await _refresh_derived(session, result.linked)
            await session.commit()
            waiting_links = sum(store.waiting_links().values())
            store.commit()
    except BaseException:
        store.rollback()
        raise
    return LinksOutcome(
        path,
        scope,
        result.linked,
        result.edited,
        result.gone,
        result.waiting,
        result.untrusted + links.untrusted,
        result.foreign + links.foreign,
        relations_linked=links.relations_linked,
        parents_linked=links.parents_linked,
        links_waiting=waiting_links,
        link_problems=[problem.line() for problem in links.problems],
    )


async def _refresh_derived(session: AsyncSession, linked: list[tuple[str, int]]) -> None:
    """Bring the wiki link graph and the search chunks up to date with the linked texts.

    An import does this in its later phases; on demand it is done here, best
    effort: a failure is logged, and the backfill command can catch up.
    """
    from specivo.schemas.search import SearchSourceType
    from specivo.services.embedding_service import EmbeddingService
    from specivo.services.wiki_link_service import WikiLinkService

    issues = sorted({row_id for entity, row_id in linked if entity == "issue"})
    journals = sorted({row_id for entity, row_id in linked if entity == "journal"})
    contents = sorted({row_id for entity, row_id in linked if entity == "wiki_version"})
    pages: list[tuple[int, int]] = []
    if contents:
        pages = [
            (page_id, wiki_id)
            for page_id, wiki_id in (
                await session.execute(
                    select(WikiPage.id, WikiPage.wiki_id)
                    .join(WikiContent, WikiContent.page_id == WikiPage.id)
                    .where(WikiContent.id.in_(contents))
                    .distinct()
                )
            ).all()
        ]

    try:
        # In a savepoint, so a failure here cannot take the linked texts down with it.
        async with session.begin_nested():
            for page_id, wiki_id in pages:
                await WikiLinkService().rebuild_page_links(session, wiki_id, page_id)
            embedder = EmbeddingService()
            for source_type, ids in (
                (SearchSourceType.ISSUE, issues),
                (SearchSourceType.JOURNAL, journals),
                (SearchSourceType.WIKI_PAGE, [page_id for page_id, _ in pages]),
            ):
                for entity_id in ids:
                    await embedder.index_source(session, source_type, entity_id)
    except Exception:  # noqa: BLE001 - the texts are linked either way
        logger.exception("Could not refresh the link graph or search index for linked texts")


__all__ = [
    "LINKED_NOTE",
    "WAITING_NOTE",
    "LinksFileError",
    "LinksOutcome",
    "check_links_file",
    "link_placeholders",
    "links_path",
    "open_for_phase",
    "resolve_links_file",
    "usable",
]
