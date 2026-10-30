"""Outgoing webhook events: build payloads, write them to the outbox, dispatch after commit.

Services call one of the ``emit_*`` helpers where a change is made, so the web
UI, the REST API and MCP all emit. Nothing is ever sent from inside the
transaction:

1. :func:`emit` looks up the project's active webhooks for the event (cached in
   Redis, see :func:`active_subscriptions`). With none, it returns at once and
   the payload is never built.
2. Otherwise it builds the payload and writes one ``pending``
   ``webhook_deliveries`` row per webhook in the caller's transaction (an
   outbox): a rollback takes the rows with it.
3. ``after_commit`` queues ``deliver_webhook`` for the rows the transaction
   wrote; a rollback queues nothing.
4. The beat sweeper (``specivo.tasks.webhooks.sweep_webhook_deliveries``)
   re-queues pending rows whose ``next_attempt_at`` has passed, so a broker
   that is down at commit time delays a delivery but does not lose it.

Payload
-------
Every event is sent as JSON::

    {
      "event": "issue.updated",
      "delivery_id": "<uuid, also in X-Specivo-Delivery, the same on every retry>",
      "occurred_at": "2026-10-30T12:00:00Z",
      "project": {"key": "WEB", "name": "Website"},
      "actor": {"id": 4, "login": "alice", "display_name": "Alice Example"},
      "data": {...}
    }

``data`` per event (objects are the REST API's ``*Out`` schemas, so a
payload and ``GET`` of the same object agree):

* ``issue.created``: ``{"issue": IssueOut}``.
* ``issue.updated``: ``{"issue": IssueOut, "changes": {field: {"old", "new"}},
  "journal_id": int, "notes": str | null}``. ``changes`` comes from the
  history entry the update wrote: issue attributes by name (``status_id``,
  ``subject``, ...), values as history stores them (ids as strings, full text
  for ``description``). Sent for every update that wrote a history entry,
  including one that only added notes.
* ``issue.commented``: ``{"issue": IssueOut, "comment": JournalOut}``.
* ``issue.deleted``: ``{"issue": IssueOut}``, the issue as it was before the
  delete.
* ``wiki.page.updated``: ``{"page": WikiPageWithContent, "comments": str |
  null, "created": bool}``; sent when a page is created and for every new
  version.
* ``ping``: ``{"webhook_id": int, "message": str}``, sent by "Send test".

A private issue (and everything that happens on it) and a private note are
sent only to webhooks with ``include_private`` on.

Headers: ``X-Specivo-Event``, ``X-Specivo-Delivery`` and
``X-Specivo-Signature: sha256=<hex HMAC-SHA256 of the raw body>``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import random
import secrets
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, joinedload

from specivo.core.constants import (
    WEBHOOK_BACKOFF_BASE_SECONDS,
    WEBHOOK_BACKOFF_MAX_SECONDS,
    WEBHOOK_LEASE_SECONDS,
    WEBHOOK_SUBSCRIPTION_CACHE_SECONDS,
)
from specivo.core.redis import get_redis
from specivo.core.utils import utcnow
from specivo.models.issue import Issue
from specivo.models.journal import Journal, JournalDetail
from specivo.models.project import Project
from specivo.models.user import User
from specivo.models.webhook import DELIVERY_PENDING, Webhook, WebhookDelivery
from specivo.models.wiki import WikiContent, WikiPage
from specivo.schemas.common import IdName
from specivo.schemas.issue import IssueOut, issue_out
from specivo.schemas.journal import JournalOut
from specivo.schemas.webhook import WebhookEvent
from specivo.schemas.wiki import WikiPageWithContent, page_with_content

logger = logging.getLogger(__name__)

#: ``session.info`` key: ids of the deliveries this transaction wrote.
_PENDING_KEY = "webhook_deliveries_pending"
#: ``session.info`` key: projects whose webhooks this transaction changed.
_DIRTY_KEY = "webhook_subscriptions_dirty"
#: ``session.info`` key: when true, the session emits no events (the import pipeline).
SUPPRESS_KEY = "webhook_events_suppressed"
_CACHE_PREFIX = "webhook_subs"

DataBuilder = Callable[[], Awaitable[dict[str, Any]]]

_background: set[asyncio.Task[None]] = set()


# ---------------------------------------------------------------------------
# Payload building (pure)
# ---------------------------------------------------------------------------


class ProjectRef(BaseModel):
    key: str
    name: str


class ActorRef(BaseModel):
    id: int
    login: str
    display_name: str


class WebhookEnvelope(BaseModel):
    event: str
    delivery_id: uuid.UUID
    occurred_at: datetime
    project: ProjectRef
    actor: ActorRef | None
    data: dict[str, Any]


def build_envelope(
    event_name: WebhookEvent,
    delivery_uuid: uuid.UUID,
    occurred_at: datetime,
    *,
    project: ProjectRef,
    actor: ActorRef | None,
    data: dict[str, Any],
) -> dict[str, Any]:
    """The JSON body sent for one delivery."""
    return WebhookEnvelope(
        event=event_name.value,
        delivery_id=delivery_uuid,
        occurred_at=occurred_at,
        project=project,
        actor=actor,
        data=data,
    ).model_dump(mode="json")


def actor_ref(user: User | None) -> ActorRef | None:
    if user is None:
        return None
    return ActorRef(id=user.id, login=user.login, display_name=user.display_name)


def issue_event_data(
    issue: IssueOut,
    *,
    changes: dict[str, dict[str, str | None]] | None = None,
    journal_id: int | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """``data`` for ``issue.created``, ``issue.deleted`` and (with ``changes``) ``issue.updated``."""
    data: dict[str, Any] = {"issue": issue.model_dump(mode="json")}
    if changes is not None:
        data.update(changes=changes, journal_id=journal_id, notes=notes)
    return data


def comment_event_data(issue: IssueOut, journal: JournalOut) -> dict[str, Any]:
    """``data`` for ``issue.commented``."""
    return {"issue": issue.model_dump(mode="json"), "comment": journal.model_dump(mode="json")}


def wiki_event_data(page: WikiPageWithContent, *, comments: str | None, created: bool) -> dict[str, Any]:
    """``data`` for ``wiki.page.updated``."""
    return {"page": page.model_dump(mode="json"), "comments": comments, "created": created}


def changes_from_details(
    details: Iterable[tuple[str, str, str | None, str | None]],
) -> dict[str, dict[str, str | None]]:
    """``issue.updated`` change set from ``(property, prop_key, old, new)`` journal details.

    Attribute changes are keyed by the attribute; anything else (an attachment,
    a relation) by ``property:prop_key`` so it cannot collide with one.
    """
    changes: dict[str, dict[str, str | None]] = {}
    for prop, prop_key, old, new in details:
        key = prop_key if prop == "attr" else f"{prop}:{prop_key}"
        changes[key] = {"old": old, "new": new}
    return changes


def signature_header(secret: str, body: bytes) -> str:
    """``X-Specivo-Signature`` value: ``sha256=`` and the hex HMAC-SHA256 of *body*."""
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def encode_body(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def generate_secret() -> str:
    """A new webhook secret: 32 random bytes, hex encoded."""
    return secrets.token_hex(32)


def backoff_seconds(attempt: int, rand: Callable[[], float] = random.random) -> float:
    """Delay before retry *attempt* (1 = first retry): exponential, capped, plus up to 25% jitter."""
    base = float(min(WEBHOOK_BACKOFF_BASE_SECONDS * 2 ** max(attempt - 1, 0), WEBHOOK_BACKOFF_MAX_SECONDS))
    return base * (1 + 0.25 * rand())


# ---------------------------------------------------------------------------
# Subscriptions, cached per project
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Subscription:
    webhook_id: int
    events: frozenset[str]
    include_private: bool

    def wants(self, event_name: str, private: bool) -> bool:
        return event_name in self.events and (self.include_private or not private)


def _cache_key(project_id: int) -> str:
    return f"{_CACHE_PREFIX}:{project_id}"


async def active_subscriptions(session: AsyncSession, project_id: int) -> list[Subscription]:
    """The project's active webhooks, from Redis when cached there.

    Every issue and wiki change asks this, so the common answer ("none") costs
    one Redis read. Changes to a project's webhooks drop the entry
    (:func:`invalidate_subscriptions`); the TTL bounds anything missed.
    """
    try:
        cached = await (await get_redis()).get(_cache_key(project_id))
        if cached is not None:
            return [
                Subscription(webhook_id=row[0], events=frozenset(row[1]), include_private=bool(row[2]))
                for row in json.loads(cached)
            ]
    except Exception:  # noqa: BLE001 - Redis unavailable: fall through to the database
        logger.debug("Webhook subscription cache unavailable", exc_info=True)

    rows = (
        await session.execute(
            select(Webhook.id, Webhook.events, Webhook.include_private).where(
                Webhook.project_id == project_id, Webhook.is_active.is_(True)
            )
        )
    ).all()
    subs = [
        Subscription(webhook_id=row.id, events=frozenset(row.events or []), include_private=row.include_private)
        for row in rows
    ]
    try:
        await (await get_redis()).set(
            _cache_key(project_id),
            json.dumps([[s.webhook_id, sorted(s.events), s.include_private] for s in subs]),
            ex=WEBHOOK_SUBSCRIPTION_CACHE_SECONDS,
        )
    except Exception:  # noqa: BLE001
        logger.debug("Could not cache webhook subscriptions", exc_info=True)
    return subs


async def invalidate_subscriptions(session: AsyncSession | None, project_id: int) -> None:
    """Drop the cached webhook list of *project_id*, now and again after *session* commits.

    The second drop covers a concurrent request that re-read the list before
    this transaction committed.
    """
    if session is not None:
        session.info.setdefault(_DIRTY_KEY, set()).add(project_id)
    await _drop_cached([project_id])


async def _drop_cached(project_ids: Iterable[int]) -> None:
    keys = [_cache_key(pid) for pid in project_ids]
    if not keys:
        return
    try:
        await (await get_redis()).delete(*keys)
    except Exception:  # noqa: BLE001 - the TTL bounds a stale entry
        logger.debug("Could not drop cached webhook subscriptions", exc_info=True)


# ---------------------------------------------------------------------------
# Outbox
# ---------------------------------------------------------------------------


def suppress_webhook_events(session: AsyncSession | Session) -> None:
    """Stop *session* from emitting webhook events."""
    session.info[SUPPRESS_KEY] = True


async def emit(
    session: AsyncSession,
    project_id: int,
    event_name: WebhookEvent,
    actor: User | None,
    build: DataBuilder,
    *,
    private: bool = False,
) -> list[WebhookDelivery]:
    """Write a pending delivery of *event_name* for each subscribed active webhook.

    ``build`` returns the event's ``data``; it is only called when at least
    one webhook wants the event. ``private`` limits the event to webhooks with
    ``include_private`` on. The rows are dispatched after the transaction
    commits.
    """
    if session.info.get(SUPPRESS_KEY):
        return []
    targets = [s.webhook_id for s in await active_subscriptions(session, project_id) if s.wants(event_name, private)]
    if not targets:
        return []
    return await _write_deliveries(session, project_id, targets, event_name, actor, await build())


async def emit_to_webhook(
    session: AsyncSession, webhook: Webhook, event_name: WebhookEvent, actor: User | None, data: dict[str, Any]
) -> WebhookDelivery:
    """Write a pending delivery to one webhook, whatever it subscribes to (the test event)."""
    rows = await _write_deliveries(session, webhook.project_id, [webhook.id], event_name, actor, data)
    return rows[0]


async def _write_deliveries(
    session: AsyncSession,
    project_id: int,
    webhook_ids: list[int],
    event_name: WebhookEvent,
    actor: User | None,
    data: dict[str, Any],
) -> list[WebhookDelivery]:
    project = await session.get(Project, project_id)
    if project is None:
        return []
    now = utcnow()
    ref = ProjectRef(key=project.key, name=project.name)
    rows = []
    for webhook_id in webhook_ids:
        delivery_uuid = uuid.uuid4()
        row = WebhookDelivery(
            webhook_id=webhook_id,
            delivery_uuid=delivery_uuid,
            event=event_name.value,
            payload=build_envelope(event_name, delivery_uuid, now, project=ref, actor=actor_ref(actor), data=data),
            status=DELIVERY_PENDING,
            attempts=0,
            # The task queued after commit owns the row until then; past it the sweeper takes over.
            next_attempt_at=now + timedelta(seconds=WEBHOOK_LEASE_SECONDS),
        )
        session.add(row)
        rows.append(row)
    await session.flush()
    session.info.setdefault(_PENDING_KEY, []).extend(row.id for row in rows)
    logger.info("Webhook event %s for project %d: %d delivery(ies) pending", event_name.value, project_id, len(rows))
    return rows


def dispatch_deliveries(delivery_ids: Iterable[tuple[int, int]]) -> int:
    """Queue ``deliver_webhook`` for ``(delivery id, attempts so far)`` pairs; never raises.

    Returns how many were queued. A row that could not be queued stays
    pending and the sweeper queues it once its ``next_attempt_at`` passes.
    """
    from specivo.tasks.webhooks import deliver_webhook

    queued = 0
    for delivery_id, attempts in delivery_ids:
        try:
            deliver_webhook.apply_async(args=[delivery_id, attempts], retry=False)
        except Exception as exc:  # noqa: BLE001 - a dead broker must not fail the request
            logger.warning(
                "Could not queue webhook delivery %d (%s); the sweeper will retry it", delivery_id, type(exc).__name__
            )
            continue
        queued += 1
    return queued


@event.listens_for(Session, "after_commit")
def _after_commit(session: Session) -> None:
    pending: list[int] | None = session.info.pop(_PENDING_KEY, None)
    if pending:
        dispatch_deliveries((delivery_id, 0) for delivery_id in pending)
    dirty: set[int] | None = session.info.pop(_DIRTY_KEY, None)
    if dirty:
        try:
            task = asyncio.get_running_loop().create_task(_drop_cached(dirty))
        except RuntimeError:
            return  # no loop (sync use): the TTL bounds the stale entry
        # Keep a reference until it finishes, or the loop may drop the task.
        _background.add(task)
        task.add_done_callback(_background.discard)


@event.listens_for(Session, "after_soft_rollback")
def _after_soft_rollback(session: Session, previous_transaction: Any) -> None:
    # Only the outermost rollback discards the rows; a task queued for a row
    # rolled back in a savepoint finds nothing and stops.
    if previous_transaction.parent is None:
        session.info.pop(_PENDING_KEY, None)


# ---------------------------------------------------------------------------
# Emission points, called by the services
# ---------------------------------------------------------------------------


async def _issue_out(session: AsyncSession, issue_id: int) -> IssueOut:
    from specivo.services.computed_metadata_service import computed_values, load_project_settings

    issue = (
        await session.execute(
            select(Issue)
            .where(Issue.id == issue_id)
            .options(
                joinedload(Issue.tracker),
                joinedload(Issue.status),
                joinedload(Issue.priority),
                joinedload(Issue.category),
                joinedload(Issue.author),
                joinedload(Issue.assigned_to),
                joinedload(Issue.fixed_version),
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    return issue_out(issue, computed_values(await load_project_settings(session, issue.project_id)))


async def emit_issue_created(session: AsyncSession, issue: Issue, actor: User | None) -> None:
    async def build() -> dict[str, Any]:
        await session.flush()
        return issue_event_data(await _issue_out(session, issue.id))

    await emit(session, issue.project_id, WebhookEvent.ISSUE_CREATED, actor, build, private=issue.is_private)


async def emit_issue_updated(
    session: AsyncSession, issue: Issue, journal: Journal, actor: User | None, *, was_private: bool
) -> None:
    """``was_private``: the issue was private before the update; a change of privacy either way is private."""

    async def build() -> dict[str, Any]:
        await session.flush()
        details = (
            await session.execute(
                select(JournalDetail.property, JournalDetail.prop_key, JournalDetail.old_value, JournalDetail.new_value)
                .where(JournalDetail.journal_id == journal.id)
                .order_by(JournalDetail.id)
            )
        ).all()
        return issue_event_data(
            await _issue_out(session, issue.id),
            changes=changes_from_details((d[0], d[1], d[2], d[3]) for d in details),
            journal_id=journal.id,
            notes=journal.notes,
        )

    private = issue.is_private or was_private
    await emit(session, issue.project_id, WebhookEvent.ISSUE_UPDATED, actor, build, private=private)


async def emit_issue_commented(session: AsyncSession, issue: Issue, journal: Journal, actor: User) -> None:
    async def build() -> dict[str, Any]:
        await session.flush()
        comment = JournalOut(
            id=journal.id,
            sequence=journal.sequence,
            issue_id=journal.issue_id,
            user=IdName(id=actor.id, name=actor.display_name),
            notes=journal.notes,
            is_private=journal.is_private,
            reply_to_id=journal.reply_to_id,
            is_resolved=journal.is_resolved,
            created_at=journal.created_at,
            updated_at=journal.updated_at,
        )
        return comment_event_data(await _issue_out(session, issue.id), comment)

    private = issue.is_private or journal.is_private
    await emit(session, issue.project_id, WebhookEvent.ISSUE_COMMENTED, actor, build, private=private)


async def emit_issue_deleted(session: AsyncSession, issue: Issue, actor: User | None) -> None:
    """Call before the delete: the payload is the issue as it was."""

    async def build() -> dict[str, Any]:
        return issue_event_data(await _issue_out(session, issue.id))

    await emit(session, issue.project_id, WebhookEvent.ISSUE_DELETED, actor, build, private=issue.is_private)


async def emit_wiki_page_updated(
    session: AsyncSession,
    project_id: int,
    page: WikiPage,
    content: WikiContent,
    actor: User,
    *,
    created: bool,
) -> None:
    async def build() -> dict[str, Any]:
        await session.flush()
        await session.refresh(page)
        await session.refresh(content)
        return wiki_event_data(page_with_content(page, content, actor), comments=content.comments, created=created)

    await emit(session, project_id, WebhookEvent.WIKI_PAGE_UPDATED, actor, build)
