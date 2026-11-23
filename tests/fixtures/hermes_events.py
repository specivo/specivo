"""Helpers for Hermes event receiver tests: payloads in Hermes' wire format and signed posts.

The body follows ``agent/outbound_webhooks.py`` of Hermes Agent: the shell-hook
payload (``tool_name``, ``tool_input``, ``session_id``, ``cwd``, ``profile``,
``extra``) plus ``hook_event_name``, ``delivery_id`` and ``timestamp``; the
signature is ``sha256=<hex HMAC-SHA256 of the raw body>``. Content is neutral.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from httpx import AsyncClient, Response

#: A string that must never reach the database: put into tool inputs, results, commands and goals.
CANARY = "canary-7f3a-do-not-store"


def iso(moment: datetime) -> str:
    """A timestamp as Hermes writes it (``...Z``)."""
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def payload(
    event: str,
    session_id: str,
    *,
    tool_name: str | None = None,
    tool_input: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
    delivery_id: str | None = None,
    timestamp: str | None = None,
    age: timedelta = timedelta(0),
) -> dict[str, Any]:
    """One Hermes outbound webhook body."""
    return {
        "hook_event_name": event,
        "profile": "default",
        "tool_name": tool_name,
        "tool_input": tool_input,
        "session_id": session_id,
        "cwd": "/home/sample/project",
        "extra": {**(extra or {}), "kanban_task_id": None},
        "delivery_id": delivery_id or uuid.uuid4().hex,
        "timestamp": timestamp if timestamp is not None else iso(datetime.now(UTC) - age),
    }


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def headers(body: bytes, secret: str | None, event: str = "post_tool_call", delivery: str = "") -> dict[str, str]:
    """The headers Hermes sends; no signature header when *secret* is ``None``."""
    out = {
        "Content-Type": "application/json",
        "User-Agent": "Hermes-Agent-Outbound-Webhook",
        "X-Hermes-Event": event,
        "X-Hermes-Delivery": delivery or uuid.uuid4().hex,
    }
    if secret is not None:
        out["X-Hermes-Signature-256"] = sign(secret, body)
    return out


def event_path(token: str, slash: bool = True) -> str:
    return f"/api/v1/hermes/events/{token}" + ("/" if slash else "")


async def post_event(
    client: AsyncClient, token: str, body: dict[str, Any] | bytes, secret: str | None, *, slash: bool = True
) -> Response:
    """Post *body* (a payload, or raw bytes) to the receiver, signed with *secret* (unsigned for ``None``)."""
    raw = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
    event = body.get("hook_event_name", "unknown") if isinstance(body, dict) else "unknown"
    return await client.post(event_path(token, slash), content=raw, headers=headers(raw, secret, event))
