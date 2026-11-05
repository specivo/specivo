"""Keep the entity link graph in step with content: queue a rebuild after each commit.

Like the search index (``search_index_service``), the graph follows content
through SQLAlchemy session events rather than calls in each service, so every
surface that changes text through ORM objects (web, REST, MCP, recurring
generation, issue move, imports run outside the pipeline) is covered:

* ``after_flush`` records which link sources the flush touched in
  ``session.info``: an issue whose description or project changed, a comment
  whose text changed, a wiki page whose content, slug, title or trash state
  changed, and new attachments (a filename link written before the upload
  can now resolve). Deletes need nothing: the rows go with their source
  (``ON DELETE CASCADE``) and links to a deleted target become unresolved
  (``ON DELETE SET NULL``).
* ``after_commit`` queues one ``rebuild_entity_links`` task for everything the
  transaction touched. A rollback queues nothing, and the task reads the
  committed text, so it never sees a half-written change.

Paths that change rows with bulk ``UPDATE`` statements call
:func:`queue_entity_links` themselves. A session marked with
:func:`defer_entity_links` records nothing; the import pipeline uses it and
rebuilds the graph of everything it imported once, in process.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from sqlalchemy import event, inspect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from specivo.models.attachment import Attachment
from specivo.models.issue import Issue
from specivo.models.journal import Journal
from specivo.models.wiki import WikiContent, WikiPage

logger = logging.getLogger(__name__)

SourceKey = tuple[str, int]

#: ``session.info`` key: when true, the session queues no link rebuilds.
DEFER_KEY = "entity_links_deferred"
#: ``session.info`` key: link sources touched by the transaction so far.
_PENDING_KEY = "entity_links_pending"

# Keys the task expands rather than rebuilds directly.
#: Every comment of an issue (its project changed, so ``[[Page]]`` means another wiki).
ISSUE_COMMENTS = "issue_comments"
#: A new attachment: the sources with an unresolved link naming its filename.
ATTACHMENT = "attachment"

_ISSUE_ATTRS = ("description", "project_id", "project_key", "sequence_number")
_JOURNAL_ATTRS = ("notes", "issue_id", "wiki_page_id")
_WIKI_PAGE_ATTRS = ("slug", "title", "deleted_at", "wiki_id")


def defer_entity_links(session: AsyncSession | Session) -> None:
    """Stop *session* from queueing link rebuilds; the caller rebuilds the graph itself."""
    session.info[DEFER_KEY] = True


def _changed(obj: object, attrs: tuple[str, ...]) -> bool:
    state = inspect(obj)
    if state is None:
        return False
    return any(state.attrs[name].history.has_changes() for name in attrs)


def collect(session: Session) -> set[SourceKey]:
    """Return the link sources a flush touched, from the session's pre-flush state."""
    keys: set[SourceKey] = set()

    for obj in session.new:
        if isinstance(obj, Issue):
            keys.add(("issue", obj.id))
        elif isinstance(obj, Journal):
            if obj.notes:
                keys.add(("journal", obj.id))
        elif isinstance(obj, WikiContent):
            keys.add(("wiki_page", obj.page_id))
        elif isinstance(obj, WikiPage):
            keys.add(("wiki_page", obj.id))
        elif isinstance(obj, Attachment):
            keys.add((ATTACHMENT, obj.id))

    for obj in session.dirty:
        if isinstance(obj, Issue):
            if _changed(obj, ("project_id",)):
                keys.add(("issue", obj.id))
                keys.add((ISSUE_COMMENTS, obj.id))
            elif _changed(obj, _ISSUE_ATTRS):
                keys.add(("issue", obj.id))
        elif isinstance(obj, Journal) and _changed(obj, _JOURNAL_ATTRS):
            keys.add(("journal", obj.id))
        elif isinstance(obj, WikiContent) and _changed(obj, ("text", "page_id")):
            keys.add(("wiki_page", obj.page_id))
        elif isinstance(obj, WikiPage) and _changed(obj, _WIKI_PAGE_ATTRS):
            keys.add(("wiki_page", obj.id))

    return keys


def _add_pending(session: Session, keys: Iterable[SourceKey]) -> None:
    pending: set[SourceKey] = session.info.setdefault(_PENDING_KEY, set())
    pending.update(keys)


@event.listens_for(Session, "after_flush")
def _after_flush(session: Session, _flush_context: Any) -> None:
    if session.info.get(DEFER_KEY):
        return
    keys = collect(session)
    if keys:
        _add_pending(session, keys)


@event.listens_for(Session, "after_commit")
def _after_commit(session: Session) -> None:
    pending: set[SourceKey] | None = session.info.pop(_PENDING_KEY, None)
    if pending:
        dispatch_entity_links(pending)


@event.listens_for(Session, "after_soft_rollback")
def _after_soft_rollback(session: Session, previous_transaction: Any) -> None:
    # Only the outermost rollback discards the work; a rolled-back savepoint's
    # sources are re-read by the task, which finds them as committed.
    if previous_transaction.parent is None:
        session.info.pop(_PENDING_KEY, None)


def queue_entity_links(session: AsyncSession | Session, keys: Iterable[SourceKey]) -> None:
    """Queue link rebuilds for sources changed by bulk statements the session events cannot see."""
    if session.info.get(DEFER_KEY):
        return
    target = session.sync_session if isinstance(session, AsyncSession) else session
    _add_pending(target, [(str(t), int(i)) for t, i in keys])


def dispatch_entity_links(keys: Iterable[SourceKey]) -> bool:
    """Queue one ``rebuild_entity_links`` task for *keys*; never raises.

    A dead broker must not fail the request that changed the text: the link
    graph is derived data, and ``python -m specivo.cli.rebuild_links``
    catches up.
    """
    payload = [[source_type, source_id] for source_type, source_id in sorted(set(keys))]
    if not payload:
        return False
    try:
        from specivo.tasks.entity_links import rebuild_entity_links

        rebuild_entity_links.apply_async(args=[payload], retry=False)
    except Exception as exc:  # noqa: BLE001 - a dead broker must not fail the request
        logger.warning(
            "Could not queue a link graph rebuild for %d source(s) (%s): %s",
            len(payload),
            ", ".join(f"{t} {i}" for t, i in payload[:5]),
            exc,
        )
        return False
    return True
