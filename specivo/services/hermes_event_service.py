"""HermesEventService — verify Hermes outbound webhooks and turn them into agent sessions and audit rows.

Hermes Agent (``agent/outbound_webhooks.py``) posts one JSON body per hook
firing: ``{hook_event_name, profile, tool_name, tool_input, session_id, cwd,
extra: {...}, delivery_id, timestamp}``, signed with HMAC-SHA256 over the raw
body in ``X-Hermes-Signature-256: sha256=<hex>``. It retries once on a
connection error or a 5xx and treats every 4xx as final.

This module holds the pieces of the receiver that do not touch HTTP:

- :func:`verify_signature` and :func:`check_timestamp`, the checks on a body;
- :func:`map_event`, a pure function from a payload to what Specivo keeps of
  it (an audit event type and metadata-only details, never tool arguments,
  results, approval commands, goals or summaries);
- :meth:`HermesEventService.record_delivery`, the redelivery guard;
- :meth:`HermesEventService.apply`, which finds or opens the account's agent
  session(s) for the event and writes the audit row.

The receiver never writes AI usage or cost: Hermes' queue drops events under
load and at exit, so cost comes from the usage reporter instead.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import ValidationError
from specivo.core.utils import utcnow
from specivo.models.agent_session import AgentSession
from specivo.models.hermes_receiver import HermesDelivery, HermesReceiver
from specivo.models.issue import Issue
from specivo.models.user import User
from specivo.services.agent_session_service import (
    CLIENT_TYPE_WEBHOOK,
    PROJECT_FREE_TOOLS,
    AgentSessionService,
    ClientInfo,
    check_external_session_id,
)
from specivo.services.security_audit_service import AuditEvent, SecurityAuditService

#: How far an event's ``timestamp`` may be from the server clock.
TIMESTAMP_WINDOW = timedelta(minutes=10)

#: How long accepted delivery ids are kept for the redelivery check.
DELIVERY_RETENTION_DAYS = 7

#: The client name of a session a receiver opens.
HERMES_CLIENT = ClientInfo(name="hermes-agent")

_SIGNATURE_PREFIX = "sha256="
_DELIVERY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
_TEXT_MAX = 200
_LIST_MAX = 50

# Outcomes of a valid delivery, answered with 200.
STATUS_ACCEPTED = "accepted"
STATUS_DUPLICATE = "duplicate"
STATUS_IGNORED = "ignored"

# Kinds of mapped events: what they do to sessions besides the audit row.
KIND_SESSION_START = "session_start"
KIND_TOOL_CALL = "tool_call"
KIND_APPROVAL = "approval"
KIND_SUBAGENT_START = "subagent_start"
KIND_SUBAGENT_STOP = "subagent_stop"
KIND_SESSION_END = "session_end"


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """Whether *header* (``sha256=<hex>``) is the HMAC-SHA256 of *body* under *secret*."""
    if not header or not header.startswith(_SIGNATURE_PREFIX):
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len(_SIGNATURE_PREFIX) :].strip().lower())


def check_timestamp(value: Any, now: datetime | None = None) -> bool:
    """Whether *value* (ISO 8601, as Hermes writes it) lies within ``TIMESTAMP_WINDOW`` of *now*.

    A missing, malformed or naive timestamp fails: it is part of the signed
    body, so without it a captured delivery could be replayed for ever.
    """
    if not isinstance(value, str) or not value:
        return False
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return False
    if stamp.tzinfo is None:
        return False
    return abs((now or utcnow()) - stamp.astimezone(UTC)) <= TIMESTAMP_WINDOW


def delivery_id_of(payload: dict[str, Any]) -> str | None:
    """The payload's ``delivery_id`` when it has the expected shape, else ``None``."""
    value = payload.get("delivery_id")
    if isinstance(value, str) and _DELIVERY_ID.fullmatch(value):
        return value
    return None


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value[:_TEXT_MAX]
    return None


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return value
    return None


def _flag(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _texts(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    out = [item[:_TEXT_MAX] for item in value if isinstance(item, str) and item][:_LIST_MAX]
    return out or None


def _tool_names(history: Any) -> list[str] | None:
    """The tool names of a ``subagent_stop`` ``tool_call_history``, in order; nothing else of it."""
    if not isinstance(history, list):
        return None
    names = [_text(item.get("tool_name")) for item in history if isinstance(item, dict)]
    out = [name for name in names if name][:_LIST_MAX]
    return out or None


def _keep(**fields: Any) -> dict[str, Any]:
    return {key: value for key, value in fields.items() if value is not None}


@dataclass(frozen=True)
class MappedEvent:
    """What Specivo keeps of one Hermes event.

    ``session_id`` is the Hermes session the audit row belongs to (for
    subagent events, the parent's); ``child_session_id`` is the subagent's.
    ``details`` holds metadata only.
    """

    hermes_event: str
    kind: str
    audit_event: AuditEvent
    session_id: str
    details: dict[str, Any] = field(default_factory=dict)
    child_session_id: str | None = None


def _session_start(payload: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    return _keep(
        model=_text(extra.get("model")), platform=_text(extra.get("platform")), profile=_text(payload.get("profile"))
    )


def _tool_call(payload: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    tool = _text(payload.get("tool_name"))
    if tool in PROJECT_FREE_TOOLS:
        # Those names mark project-free MCP calls on a project's timeline; a Hermes tool is not one.
        tool = f"hermes:{tool}"
    return _keep(
        tool=tool,
        status=_text(extra.get("status")),
        error_type=_text(extra.get("error_type")),
        duration_ms=_number(extra.get("duration_ms")),
        tool_call_id=_text(extra.get("tool_call_id")),
        turn_id=_text(extra.get("turn_id")),
    )


def _approval_request(_payload: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    return _keep(
        pattern_key=_text(extra.get("pattern_key")),
        pattern_keys=_texts(extra.get("pattern_keys")),
        surface=_text(extra.get("surface")),
    )


def _approval_response(_payload: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    return _keep(
        choice=_text(extra.get("choice")),
        decided_by=_text(extra.get("decided_by")),
        pattern_key=_text(extra.get("pattern_key")),
    )


def _subagent_start(_payload: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    return _keep(
        child_session_id=_text(extra.get("child_session_id")),
        child_role=_text(extra.get("child_role")),
        child_subagent_id=_text(extra.get("child_subagent_id")),
    )


def _subagent_stop(_payload: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    return _keep(
        child_session_id=_text(extra.get("child_session_id")),
        child_role=_text(extra.get("child_role")),
        child_status=_text(extra.get("child_status")),
        duration_ms=_number(extra.get("duration_ms")),
        tools=_tool_names(extra.get("tool_call_history")),
    )


def _session_end(_payload: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    return _keep(completed=_flag(extra.get("completed")), interrupted=_flag(extra.get("interrupted")))


#: The mapping table: Hermes event -> (kind, audit event, the details it keeps).
EVENT_MAP: dict[str, tuple[str, AuditEvent, Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]]] = {
    "on_session_start": (KIND_SESSION_START, AuditEvent.AGENT_SESSION_STARTED, _session_start),
    "post_tool_call": (KIND_TOOL_CALL, AuditEvent.AGENT_TOOL_CALLED, _tool_call),
    "pre_approval_request": (KIND_APPROVAL, AuditEvent.AGENT_APPROVAL_REQUESTED, _approval_request),
    "post_approval_response": (KIND_APPROVAL, AuditEvent.AGENT_APPROVAL_RESOLVED, _approval_response),
    "subagent_start": (KIND_SUBAGENT_START, AuditEvent.AGENT_SUBAGENT_STARTED, _subagent_start),
    "subagent_stop": (KIND_SUBAGENT_STOP, AuditEvent.AGENT_SUBAGENT_STOPPED, _subagent_stop),
    "on_session_end": (KIND_SESSION_END, AuditEvent.AGENT_SESSION_ENDED, _session_end),
}


def map_event(payload: dict[str, Any]) -> MappedEvent | None:
    """Map a Hermes payload to the audit event Specivo writes, or ``None`` when it is not kept.

    Events outside ``EVENT_MAP`` (``post_api_request``, ``pre_llm_call`` and
    the rest) and events without a session id are not kept. The raw
    ``tool_input``, the tool ``result`` and ``error_message``, approval
    ``command`` and ``description``, ``child_goal`` and ``child_summary`` are
    never read into the result, and neither is ``cwd``.
    """
    event = payload.get("hook_event_name")
    session_id = payload.get("session_id")
    if not isinstance(event, str) or event not in EVENT_MAP:
        return None
    if not isinstance(session_id, str) or not session_id:
        return None
    extra = payload.get("extra")
    extra = extra if isinstance(extra, dict) else {}
    kind, audit_event, keep = EVENT_MAP[event]
    details = keep(payload, extra)
    child = details.get("child_session_id") if kind in (KIND_SUBAGENT_START, KIND_SUBAGENT_STOP) else None
    return MappedEvent(event, kind, audit_event, session_id, details, child)


@dataclass(frozen=True)
class ApplyResult:
    """The outcome of a verified, first-seen delivery."""

    status: str
    agent_session_id: int | None = None


class HermesEventService:
    """Stateless service applying verified Hermes events."""

    def __init__(self) -> None:
        self._sessions = AgentSessionService()
        self._audit = SecurityAuditService()

    @staticmethod
    async def record_delivery(session: AsyncSession, receiver: HermesReceiver, delivery_id: str) -> bool:
        """Remember *delivery_id* for *receiver*; ``False`` when it was seen before."""
        stmt = (
            pg_insert(HermesDelivery)
            .values(receiver_id=receiver.id, delivery_id=delivery_id, received_at=utcnow())
            .on_conflict_do_nothing(index_elements=["receiver_id", "delivery_id"])
            .returning(HermesDelivery.delivery_id)
        )
        return (await session.execute(stmt)).scalar_one_or_none() is not None

    async def apply(
        self, session: AsyncSession, receiver: HermesReceiver, payload: dict[str, Any], delivery_id: str
    ) -> ApplyResult:
        """Apply a verified, first-seen event of *receiver* and write its audit row.

        Sessions are looked up and created among the receiver's account's
        rows only: a payload naming another account's session id opens a
        session of this account instead and never touches the other one.
        """
        receiver.last_event_at = utcnow()
        mapped = map_event(payload)
        if mapped is None:
            return ApplyResult(STATUS_IGNORED)
        user = await session.get(User, receiver.user_id)
        if user is None:  # pragma: no cover - the FK is RESTRICT
            return ApplyResult(STATUS_IGNORED)
        try:
            return await self._apply(session, receiver, user, mapped, delivery_id)
        except ValidationError:
            # A session id of a shape Specivo does not store: valid, but nothing to attach to.
            return ApplyResult(STATUS_IGNORED)

    async def _apply(
        self, session: AsyncSession, receiver: HermesReceiver, user: User, mapped: MappedEvent, delivery_id: str
    ) -> ApplyResult:
        agent_session = await self._open(session, receiver, user, mapped.session_id)
        details = dict(mapped.details)

        if mapped.kind == KIND_SUBAGENT_START and mapped.child_session_id:
            child = await self._open(session, receiver, user, mapped.child_session_id, parent=mapped.session_id)
            details["child_agent_session_id"] = child.id
        elif mapped.kind == KIND_SUBAGENT_STOP and mapped.child_session_id:
            # The child's row may have ended already (its own on_session_end): stop that one, not a new one.
            child = await self._latest_webhook_row(session, user, mapped.child_session_id) or await self._open(
                session, receiver, user, mapped.child_session_id, parent=mapped.session_id
            )
            await self._end_run(session, child)
            details["child_agent_session_id"] = child.id
        elif mapped.kind == KIND_SESSION_END:
            await self._end_run(session, agent_session)
        elif mapped.kind == KIND_TOOL_CALL:
            await session.execute(
                update(AgentSession)
                .where(AgentSession.id == agent_session.id)
                .values(tool_call_count=AgentSession.tool_call_count + 1)
                .execution_options(synchronize_session=False)
            )

        details["event"] = mapped.hermes_event
        details["delivery_id"] = delivery_id
        await self._audit.log_hermes_event(
            session,
            mapped.audit_event,
            user_id=user.id,
            agent_session_id=agent_session.id,
            project_id=await self._project_of(session, agent_session, user),
            details=details,
        )
        return ApplyResult(STATUS_ACCEPTED, agent_session.id)

    async def _end_run(self, session: AsyncSession, agent_session: AgentSession) -> None:
        """A Hermes run ended: end a session only the events opened; one the client opened over
        MCP or REST goes on (the next turn of the conversation uses it), so it is only touched."""
        if agent_session.client_type == CLIENT_TYPE_WEBHOOK:
            await self._sessions.end_session(session, agent_session)
        else:
            await self._sessions.update_activity(session, agent_session.id)

    async def _open(
        self,
        session: AsyncSession,
        receiver: HermesReceiver,
        user: User,
        external_id: str,
        parent: str | None = None,
    ) -> AgentSession:
        return await self._sessions.open_webhook_session(
            session, user, external_id, receiver_id=receiver.id, client=HERMES_CLIENT, parent_external_id=parent
        )

    async def _latest_webhook_row(self, session: AsyncSession, user: User, external_id: str) -> AgentSession | None:
        """*user*'s newest session with client session id *external_id*, ended or not, however it was opened."""
        check_external_session_id(external_id)
        rows = await self._sessions.find_by_external(session, user.id, external_id)
        return rows[0] if rows else None

    async def _project_of(self, session: AsyncSession, agent_session: AgentSession, user: User) -> int | None:
        """The project an event's audit row names: its session's issue's, while *user* may still log there.

        A session the client opened over MCP keeps the issue it named; its events name that
        project only while the account may log usage on the issue (``can_log_on``).
        """
        if agent_session.issue_id is None:
            return None
        if agent_session.client_type != CLIENT_TYPE_WEBHOOK and not await self._sessions.can_log_on(
            session, user, agent_session.issue_id
        ):
            return None
        issue = await session.get(Issue, agent_session.issue_id)
        return issue.project_id if issue is not None else None


async def purge_old_deliveries(session: AsyncSession, now: datetime | None = None) -> int:
    """Delete delivery ids older than ``DELIVERY_RETENTION_DAYS``; returns how many went."""
    cutoff = (now or utcnow()) - timedelta(days=DELIVERY_RETENTION_DAYS)
    result = await session.execute(delete(HermesDelivery).where(HermesDelivery.received_at < cutoff))
    return int(result.rowcount or 0)  # type: ignore[attr-defined]
