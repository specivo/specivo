"""Keep the search index in step with content, inside the writing transaction.

Every issue, wiki page, comment and attachment has a ``search_sources`` row and
one or more ``search_chunks`` rows. Chunks need no embedding model: the
``trg_search_chunks_search_vector`` trigger fills their full-text vector, so a
chunk is keyword-searchable as soon as its transaction commits. Embeddings are
added later by the ``embed_search_sources`` Celery task.

The chunks are written from SQLAlchemy session events rather than from each
service, because every surface (web, REST, MCP, recurring generation, issue
move, git commit refs) changes content through ORM objects:

* ``after_flush`` looks at the issues, journals, wiki pages, wiki contents and
  attachments the flush wrote, re-reads each affected source from the database
  and writes, replaces or deletes its chunks on the same connection. Unchanged
  chunk text is left alone, so its embeddings survive.
* ``after_commit`` queues one embedding task for the sources whose chunks were
  (re)written, once per source per transaction. A rollback queues nothing.

Paths that change rows with bulk ``UPDATE`` statements (soft-deleting a wiki
page's children, restoring pages) call :func:`sync_sources` themselves. A
session marked with :func:`defer_search_index` is skipped entirely; the import
pipeline uses it and indexes everything once at the end.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any, cast

from sqlalchemy import Table, and_, delete, event, insert, inspect, literal, or_, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from specivo.core.config import get_settings
from specivo.core.constants import SEARCH_EMBED_DEBOUNCE_SECONDS
from specivo.models.attachment import Attachment
from specivo.models.issue import Issue
from specivo.models.journal import Journal
from specivo.models.search import EmbeddingModel, SearchChunk, SearchSource
from specivo.models.user import User
from specivo.models.wiki import Wiki, WikiContent, WikiPage
from specivo.schemas.search import SearchSourceType
from specivo.services.chunking_service import ChunkingService

logger = logging.getLogger(__name__)

SourceKey = tuple[str, int]

#: ``session.info`` key: when true, the session does not index anything.
DEFER_KEY = "search_index_deferred"
#: ``session.info`` key: sources whose chunks were written and await embedding.
_PENDING_KEY = "search_index_pending"
#: ``session.info`` key: cached answer to "is there a model to embed with?".
_EMBEDDABLE_KEY = "search_index_embeddable"

_ISSUE = SearchSourceType.ISSUE.value
_WIKI_PAGE = SearchSourceType.WIKI_PAGE.value
_JOURNAL = SearchSourceType.JOURNAL.value
_ATTACHMENT = SearchSourceType.ATTACHMENT.value

# Attributes whose change alters a source's chunks. A journal's is_private flag
# is not here on purpose: search reads it live from ``journals``.
_ISSUE_ATTRS = ("subject", "description")
_JOURNAL_ATTRS = ("notes", "user_id", "api_key_id", "issue_id")
_WIKI_PAGE_ATTRS = ("title", "deleted_at", "wiki_id")
_ATTACHMENT_ATTRS = ("filename", "description", "file_metadata", "container_type", "container_id")

_chunking = ChunkingService()
_sources = cast(Table, SearchSource.__table__)
_chunks = cast(Table, SearchChunk.__table__)


def defer_search_index(session: AsyncSession | Session) -> None:
    """Stop *session* from indexing; the caller indexes the content itself."""
    session.info[DEFER_KEY] = True


# ---------------------------------------------------------------------------
# Reading a source's current chunks from the database
# ---------------------------------------------------------------------------


def _attachment_project_id(conn: Connection, container_type: str, container_id: int) -> int | None:
    if container_type == "Issue":
        stmt = select(Issue.project_id).where(Issue.id == container_id)
    elif container_type == "WikiPage":
        # A page in the trash takes its attachments out of search with it.
        stmt = (
            select(Wiki.project_id)
            .join(WikiPage, WikiPage.wiki_id == Wiki.id)
            .where(WikiPage.id == container_id, WikiPage.deleted_at.is_(None))
        )
    elif container_type == "Journal":
        stmt = select(Issue.project_id).join(Journal, Journal.issue_id == Issue.id).where(Journal.id == container_id)
    else:
        return None
    return conn.execute(stmt).scalar_one_or_none()


def _comment_excluded(api_key_id: int | None, is_service_account: bool | None) -> bool:
    """Apply ``SEARCH_INDEX_COMMENTS`` and ``SEARCH_EXCLUDE_BOT_COMMENTS``."""
    settings = get_settings()
    if not settings.search_index_comments:
        return True
    return bool(settings.search_exclude_bot_comments and (api_key_id is not None or is_service_account))


def load_source_chunks(conn: Connection, source_type: str, entity_id: int) -> tuple[int, list[str]] | None:
    """Return ``(project_id, chunks)`` for a source, or None when it must not be indexed.

    None covers a deleted entity, a soft-deleted wiki page, a comment the
    comment settings exclude, and an attachment whose container is gone or is
    a wiki page in the trash.
    """
    if source_type == _ISSUE:
        row = conn.execute(
            select(Issue.project_id, Issue.subject, Issue.description).where(Issue.id == entity_id)
        ).first()
        if row is None:
            return None
        return row.project_id, _chunking.chunk_issue(row.subject, row.description)

    if source_type == _WIKI_PAGE:
        page = conn.execute(
            select(WikiPage.title, WikiPage.deleted_at, Wiki.project_id)
            .join(Wiki, Wiki.id == WikiPage.wiki_id)
            .where(WikiPage.id == entity_id)
        ).first()
        if page is None or page.deleted_at is not None:
            return None
        text = conn.execute(
            select(WikiContent.text)
            .where(WikiContent.page_id == entity_id)
            .order_by(WikiContent.version.desc())
            .limit(1)
        ).scalar_one_or_none()
        return page.project_id, _chunking.chunk_wiki_page(page.title, text or "")

    if source_type == _JOURNAL:
        note = conn.execute(
            select(Journal.notes, Journal.api_key_id, Issue.project_id, User.is_service_account)
            .join(Issue, Issue.id == Journal.issue_id)
            .outerjoin(User, User.id == Journal.user_id)
            .where(Journal.id == entity_id)
        ).first()
        if note is None or _comment_excluded(note.api_key_id, note.is_service_account):
            return None
        chunks = _chunking.chunk_journal(note.notes)
        return (note.project_id, chunks) if chunks else None

    if source_type == _ATTACHMENT:
        att = conn.execute(
            select(
                Attachment.filename,
                Attachment.description,
                Attachment.file_metadata.label("file_metadata"),
                Attachment.container_type,
                Attachment.container_id,
            ).where(Attachment.id == entity_id)
        ).first()
        if att is None:
            return None
        project_id = _attachment_project_id(conn, att.container_type, att.container_id)
        if project_id is None:
            return None
        meta = att.file_metadata or {}
        description = att.description
        ai_description = meta.get("ai_description")
        if ai_description:
            description = f"{description}\n\n{ai_description}" if description else ai_description
        chunks = _chunking.chunk_attachment(att.filename, description, extracted_text=meta.get("extracted_text"))
        return project_id, chunks

    raise ValueError(f"Unknown search source type {source_type!r}")


# ---------------------------------------------------------------------------
# Writing chunks
# ---------------------------------------------------------------------------


def write_source_chunks(conn: Connection, source_type: str, entity_id: int, project_id: int, chunks: list[str]) -> bool:
    """Make the source's chunks equal *chunks*; return True when chunk rows were (re)written.

    Chunks whose text is unchanged are kept with their embeddings. A changed
    project is applied in place, and the chunks' full-text vectors are rebuilt
    for its search language.
    """
    if not chunks:
        remove_source(conn, source_type, entity_id)
        return False

    source_id = conn.execute(
        pg_insert(_sources)
        .values(source_type=source_type, entity_id=entity_id, project_id=project_id)
        .on_conflict_do_nothing(index_elements=["source_type", "entity_id"])
        .returning(_sources.c.id)
    ).scalar_one_or_none()

    if source_id is None:
        source_id, old_project_id = conn.execute(
            select(_sources.c.id, _sources.c.project_id).where(
                _sources.c.source_type == source_type, _sources.c.entity_id == entity_id
            )
        ).one()
        existing = list(
            conn.execute(
                select(_chunks.c.content).where(_chunks.c.source_id == source_id).order_by(_chunks.c.chunk_index)
            ).scalars()
        )
        if old_project_id != project_id:
            _rescope_sources(conn, [source_id], project_id)
        if existing == chunks:
            return False
        # Cascades to the stale chunks' embeddings.
        conn.execute(delete(_chunks).where(_chunks.c.source_id == source_id))

    conn.execute(
        insert(_chunks),
        [
            {
                "source_id": source_id,
                "chunk_index": idx,
                "content": text,
                "metadata_json": {"source_type": source_type, "entity_id": entity_id, "project_id": project_id},
            }
            for idx, text in enumerate(chunks)
        ],
    )
    return True


def remove_source(conn: Connection, source_type: str, entity_id: int) -> None:
    """Delete a source with its chunks and embeddings."""
    conn.execute(delete(_sources).where(_sources.c.source_type == source_type, _sources.c.entity_id == entity_id))


def _rescope_sources(conn: Connection, source_ids: list[int], project_id: int) -> None:
    """Move sources to *project_id*, re-running the chunk FTS trigger for its language."""
    if not source_ids:
        return
    conn.execute(update(_sources).where(_sources.c.id.in_(source_ids)).values(project_id=project_id))
    conn.execute(
        update(_chunks)
        .where(_chunks.c.source_id.in_(source_ids))
        .values(
            # Assigning content fires the trigger (BEFORE UPDATE OF content).
            content=_chunks.c.content,
            metadata_json=_chunks.c.metadata_json.op("||")(literal({"project_id": project_id}, type_=JSONB)),
        )
    )


def rescope_issue_children(conn: Connection, issue_id: int, project_id: int) -> None:
    """Move the comment and attachment sources of an issue to its new project."""
    journal_ids = select(Journal.id).where(Journal.issue_id == issue_id)
    attachment_ids = select(Attachment.id).where(
        or_(
            and_(Attachment.container_type == "Issue", Attachment.container_id == issue_id),
            and_(Attachment.container_type == "Journal", Attachment.container_id.in_(journal_ids)),
        )
    )
    source_ids = list(
        conn.execute(
            select(_sources.c.id).where(
                _sources.c.project_id != project_id,
                or_(
                    and_(_sources.c.source_type == _JOURNAL, _sources.c.entity_id.in_(journal_ids)),
                    and_(_sources.c.source_type == _ATTACHMENT, _sources.c.entity_id.in_(attachment_ids)),
                ),
            )
        ).scalars()
    )
    _rescope_sources(conn, source_ids, project_id)


def remove_issue_children(conn: Connection, issue_id: int, project_id: int) -> None:
    """Drop the sources left behind by a deleted issue.

    Its journals are removed by the database's cascade, so their sources are
    found as orphans within the issue's project.
    """
    conn.execute(
        delete(_sources).where(
            _sources.c.source_type == _JOURNAL,
            _sources.c.project_id == project_id,
            ~select(Journal.id).where(Journal.id == _sources.c.entity_id).exists(),
        )
    )
    conn.execute(
        delete(_sources).where(
            _sources.c.source_type == _ATTACHMENT,
            _sources.c.entity_id.in_(
                select(Attachment.id).where(Attachment.container_type == "Issue", Attachment.container_id == issue_id)
            ),
        )
    )


def sync_source(conn: Connection, source_type: str, entity_id: int) -> bool:
    """Bring one source's chunks in line with the database; True when new chunks need embedding."""
    loaded = load_source_chunks(conn, source_type, entity_id)
    if loaded is None:
        remove_source(conn, source_type, entity_id)
        return False
    project_id, chunks = loaded
    return write_source_chunks(conn, source_type, entity_id, project_id, chunks)


def sync_source_isolated(conn: Connection, source_type: str, entity_id: int) -> bool:
    """:func:`sync_source` in a savepoint, so a failure leaves the transaction usable."""
    with conn.begin_nested():
        return sync_source(conn, source_type, entity_id)


# ---------------------------------------------------------------------------
# Session plumbing
# ---------------------------------------------------------------------------


def _embeddable(session: Session, conn: Connection) -> bool:
    """Whether a default embedding model can run here; cached for the session's lifetime."""
    cached = session.info.get(_EMBEDDABLE_KEY)
    if cached is not None:
        return bool(cached)
    row = conn.execute(
        select(EmbeddingModel.provider, EmbeddingModel.model_name).where(EmbeddingModel.is_default.is_(True)).limit(1)
    ).first()
    usable = False
    if row is not None:
        from specivo.services.embedding_service import is_provider_usable

        usable = is_provider_usable(row.provider, row.model_name)
    session.info[_EMBEDDABLE_KEY] = usable
    return usable


def _sync_in_session(
    session: Session,
    keys: Iterable[SourceKey],
    rescopes: Iterable[tuple[int, int]] = (),
    deleted_issues: Iterable[tuple[int, int]] = (),
    trashed_pages: Iterable[int] = (),
) -> None:
    """Sync *keys* on the session's connection, each in its own savepoint.

    The attachments of *trashed_pages* are synced too. A failure is logged and
    leaves that source as it was; it never fails the write that triggered it.
    """
    conn = session.connection()
    pending: set[SourceKey] = session.info.setdefault(_PENDING_KEY, set())
    keys = set(keys)
    page_ids = sorted(set(trashed_pages))
    if page_ids:
        attachment_ids = conn.execute(
            select(Attachment.id).where(Attachment.container_type == "WikiPage", Attachment.container_id.in_(page_ids))
        ).scalars()
        keys.update((_ATTACHMENT, attachment_id) for attachment_id in attachment_ids)

    for issue_id, project_id in deleted_issues:
        _guarded(conn, _ISSUE, issue_id, lambda i=issue_id, p=project_id: remove_issue_children(conn, i, p))
    for issue_id, project_id in rescopes:
        _guarded(conn, _ISSUE, issue_id, lambda i=issue_id, p=project_id: rescope_issue_children(conn, i, p))

    for source_type, entity_id in sorted(keys):
        changed = _guarded(conn, source_type, entity_id, lambda t=source_type, e=entity_id: sync_source(conn, t, e))
        if changed and _embeddable(session, conn):
            pending.add((source_type, entity_id))


def _guarded(conn: Connection, source_type: str, entity_id: int, work: Any) -> Any:
    try:
        with conn.begin_nested():
            return work()
    except Exception as exc:  # noqa: BLE001 - indexing must never fail the content write
        logger.warning("Search indexing failed for %s %d: %s", source_type, entity_id, describe_error(exc))
        logger.debug("Search indexing failure for %s %d", source_type, entity_id, exc_info=True)
        return None


def describe_error(exc: BaseException) -> str:
    """Name an error without its message, which can quote the content being written.

    A database error's message includes the statement's parameters, so only its
    class and SQLSTATE are given. Log the full exception at DEBUG if needed.
    """
    name = type(exc).__name__
    if isinstance(exc, DBAPIError):
        orig = exc.orig
        code = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
        inner = type(orig).__name__ if orig is not None else None
        detail = ", ".join(part for part in (inner, f"SQLSTATE {code}" if code else None) if part)
        return f"{name} ({detail})" if detail else name
    return name


def _changed(obj: object, attrs: tuple[str, ...]) -> bool:
    state = inspect(obj)
    if state is None:
        return False
    return any(state.attrs[name].history.has_changes() for name in attrs)


def _collect(
    session: Session,
) -> tuple[set[SourceKey], list[tuple[int, int]], list[tuple[int, int]], set[int]]:
    """Find the sources a flush touched, from the session's pre-flush state.

    Also returns the wiki pages moved to or from the trash, or deleted, whose
    attachments must follow them in or out of search.
    """
    keys: set[SourceKey] = set()
    rescopes: list[tuple[int, int]] = []
    deleted_issues: list[tuple[int, int]] = []
    trashed_pages: set[int] = set()

    for obj in session.new:
        if isinstance(obj, Issue):
            keys.add((_ISSUE, obj.id))
        elif isinstance(obj, Journal):
            keys.add((_JOURNAL, obj.id))
        elif isinstance(obj, WikiContent):
            keys.add((_WIKI_PAGE, obj.page_id))
        elif isinstance(obj, WikiPage):
            keys.add((_WIKI_PAGE, obj.id))
        elif isinstance(obj, Attachment):
            keys.add((_ATTACHMENT, obj.id))

    for obj in session.dirty:
        if isinstance(obj, Issue):
            if _changed(obj, ("project_id",)):
                keys.add((_ISSUE, obj.id))
                rescopes.append((obj.id, obj.project_id))
            elif _changed(obj, _ISSUE_ATTRS):
                keys.add((_ISSUE, obj.id))
        elif isinstance(obj, Journal) and _changed(obj, _JOURNAL_ATTRS):
            keys.add((_JOURNAL, obj.id))
        elif isinstance(obj, WikiContent) and _changed(obj, ("text", "page_id")):
            keys.add((_WIKI_PAGE, obj.page_id))
        elif isinstance(obj, WikiPage) and _changed(obj, _WIKI_PAGE_ATTRS):
            keys.add((_WIKI_PAGE, obj.id))
            if _changed(obj, ("deleted_at",)):
                trashed_pages.add(obj.id)
        elif isinstance(obj, Attachment) and _changed(obj, _ATTACHMENT_ATTRS):
            keys.add((_ATTACHMENT, obj.id))

    for obj in session.deleted:
        if isinstance(obj, Issue):
            keys.add((_ISSUE, obj.id))
            deleted_issues.append((obj.id, obj.project_id))
        elif isinstance(obj, Journal):
            keys.add((_JOURNAL, obj.id))
        elif isinstance(obj, WikiContent):
            keys.add((_WIKI_PAGE, obj.page_id))
        elif isinstance(obj, WikiPage):
            keys.add((_WIKI_PAGE, obj.id))
            trashed_pages.add(obj.id)
        elif isinstance(obj, Attachment):
            keys.add((_ATTACHMENT, obj.id))

    return keys, rescopes, deleted_issues, trashed_pages


@event.listens_for(Session, "after_flush")
def _after_flush(session: Session, _flush_context: Any) -> None:
    if session.info.get(DEFER_KEY):
        return
    keys, rescopes, deleted_issues, trashed_pages = _collect(session)
    if keys:
        _sync_in_session(session, keys, rescopes, deleted_issues, trashed_pages)


@event.listens_for(Session, "after_commit")
def _after_commit(session: Session) -> None:
    session.info.pop(_EMBEDDABLE_KEY, None)
    pending: set[SourceKey] | None = session.info.pop(_PENDING_KEY, None)
    if pending:
        dispatch_embedding(pending)


@event.listens_for(Session, "after_soft_rollback")
def _after_soft_rollback(session: Session, previous_transaction: Any) -> None:
    # Only the outermost rollback discards the work; a released savepoint's
    # sources are re-read by the task, which skips anything no longer there.
    if previous_transaction.parent is None:
        session.info.pop(_PENDING_KEY, None)
        session.info.pop(_EMBEDDABLE_KEY, None)


def dispatch_embedding(sources: Iterable[SourceKey]) -> bool:
    """Queue one embedding task for *sources*; never raises.

    The short countdown coalesces a burst of edits: the task reads the chunks
    as they are when it runs, and the tasks queued by later edits then find
    nothing left to embed.
    """
    payload = [[source_type, entity_id] for source_type, entity_id in sorted(set(sources))]
    if not payload:
        return False
    try:
        from specivo.tasks.embeddings import embed_search_sources

        embed_search_sources.apply_async(args=[payload], countdown=SEARCH_EMBED_DEBOUNCE_SECONDS, retry=False)
    except Exception as exc:  # noqa: BLE001 - a dead broker must not fail the request
        logger.warning(
            "Could not queue embedding for %d search source(s) (%s); the backfill will catch up: %s",
            len(payload),
            ", ".join(f"{t} {i}" for t, i in payload[:5]),
            describe_error(exc),
        )
        logger.debug("Embedding dispatch failure", exc_info=True)
        return False
    return True


async def sync_sources(session: AsyncSession, keys: Iterable[SourceKey]) -> None:
    """Sync sources changed by bulk statements the session events cannot see.

    The attachments of the wiki pages among *keys* are synced too, since a bulk
    move to or from the trash takes them in or out of search.
    """
    if session.info.get(DEFER_KEY):
        return
    key_list = [(str(t), int(i)) for t, i in keys]
    pages = [entity_id for source_type, entity_id in key_list if source_type == _WIKI_PAGE]
    if key_list:
        await session.run_sync(lambda s: _sync_in_session(s, key_list, trashed_pages=pages))
