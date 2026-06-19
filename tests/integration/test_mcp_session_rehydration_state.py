"""Regression test: a rehydrated MCP session must be already-initialized.

Background
----------
``PersistentStreamableHTTPSessionManager`` keeps a client's
``Mcp-Session-Id`` alive across an api restart by persisting it to Redis
and minting a *fresh* ``StreamableHTTPServerTransport`` under the same id
on the first request after the restart.

The transport identity surviving is necessary but not sufficient. The MCP
SDK's per-process ``ServerSession`` holds the ``initialize`` handshake
result in ``_initialization_state``. A brand-new session starts
``NotInitialized`` and rejects every non-``initialize`` request with::

    RuntimeError: Received request before initialization was complete

which the client sees as JSON-RPC ``-32602``. A long-running client that
already finished its handshake before the restart never sends a second
``initialize``, so without restoring the initialized state the rehydrated
session is permanently stuck until a manual client reconnect.

This module drives the *real* SDK ``ServerSession`` (via the production
``mcp._mcp_server`` low-level app) end to end, unlike the heavily-mocked
``test_mcp_persistent_session_manager`` suite which stubs the session out
entirely and therefore cannot observe the initialization-state machine.
The strategy: run a full handshake on manager A, drop it (simulating the
api dying), then replay only a ``tools/list`` request carrying the old
session id on a fresh manager B and assert it succeeds rather than
returning the uninitialized-state error.
"""

from __future__ import annotations

import json
from typing import Any

import anyio
import pytest

from specivo.mcp.persistent_session_manager import (
    PersistentStreamableHTTPSessionManager,
)
from specivo.mcp.server import mcp
from specivo.mcp.session_store import RedisSessionStore
from tests.integration.test_mcp_session_store import _FakeRedis

pytestmark = [pytest.mark.integration, pytest.mark.serial]


# ---------------------------------------------------------------------------
# Minimal ASGI plumbing to push one JSON-RPC message and capture the reply
# ---------------------------------------------------------------------------


_INIT_PARAMS = {
    "protocolVersion": "2025-03-26",
    "capabilities": {},
    "clientInfo": {"name": "test", "version": "1.0"},
}


def _scope(session_id: str | None) -> dict[str, Any]:
    headers: list[tuple[bytes, bytes]] = [
        (b"content-type", b"application/json"),
        # The SDK requires the client to declare it accepts both media types.
        (b"accept", b"application/json, text/event-stream"),
        (b"user-agent", b"pytest-mcp"),
    ]
    if session_id is not None:
        headers.append((b"mcp-session-id", session_id.encode("ascii")))
    return {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": headers,
        "query_string": b"",
    }


def _make_receive(body: dict[str, Any]):  # type: ignore[no-untyped-def]
    raw = json.dumps(body).encode()

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": raw, "more_body": False}

    return receive


class _Capture:
    """Collects the ASGI response and exposes status + decoded JSON body."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def __call__(self, message: dict[str, Any]) -> None:
        self.messages.append(message)

    @property
    def status(self) -> int | None:
        for m in self.messages:
            if m["type"] == "http.response.start":
                return int(m["status"])
        return None

    @property
    def session_id(self) -> str | None:
        for m in self.messages:
            if m["type"] == "http.response.start":
                for k, v in m.get("headers", []):
                    if k.lower() == b"mcp-session-id":
                        return v.decode("ascii")
        return None

    @property
    def json_body(self) -> dict[str, Any] | None:
        raw = b"".join(m.get("body", b"") for m in self.messages if m["type"] == "http.response.body")
        if not raw:
            return None
        # json_response mode returns a single JSON object.
        return json.loads(raw)


def _make_manager(store: RedisSessionStore) -> PersistentStreamableHTTPSessionManager:
    async def provider() -> RedisSessionStore:
        return store

    return PersistentStreamableHTTPSessionManager(
        app=mcp._mcp_server,
        event_store=None,
        # JSON responses make the reply a single parseable object instead of
        # an SSE stream, which keeps the test plumbing simple.
        json_response=True,
        stateless=False,
        store_provider=provider,
    )


async def _post(
    manager: PersistentStreamableHTTPSessionManager,
    body: dict[str, Any],
    session_id: str | None,
) -> _Capture:
    cap = _Capture()
    await manager._handle_stateful_request(_scope(session_id), _make_receive(body), cap)
    return cap


# ---------------------------------------------------------------------------
# The regression test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
async def test_rehydrated_session_is_already_initialized() -> None:
    fake_redis = _FakeRedis()

    # ---- Phase 1: handshake on the original manager --------------------
    store_a = RedisSessionStore(fake_redis, ttl_seconds=300)
    manager_a = _make_manager(store_a)

    async with manager_a.run():
        init = await _post(
            manager_a,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": _INIT_PARAMS,
            },
            session_id=None,
        )
        assert init.status == 200, init.json_body
        session_id = init.session_id
        assert session_id is not None

        # Complete the handshake: notifications/initialized.
        notified = await _post(
            manager_a,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            session_id=session_id,
        )
        assert notified.status in (200, 202)

        # Sanity: a normal call works before the "restart".
        listed = await _post(
            manager_a,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            session_id=session_id,
        )
        assert listed.status == 200
        assert listed.json_body is not None
        assert "result" in listed.json_body, listed.json_body

    # Exiting run() simulates the api process dying.
    assert manager_a._server_instances == {}
    # Redis still remembers the session id.
    assert await store_a.peek(session_id) is not None

    # ---- Phase 2: fresh manager, client replays the old session id -----
    store_b = RedisSessionStore(fake_redis, ttl_seconds=300)
    manager_b = _make_manager(store_b)

    async with manager_b.run():
        # The client has no idea the server restarted; it just sends its
        # next request on the same session id. It does NOT re-initialize.
        replay = await _post(
            manager_b,
            {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
            session_id=session_id,
        )

        # Give the rehydrated session's task a tick to produce the reply.
        with anyio.move_on_after(2):
            while replay.json_body is None:
                await anyio.sleep(0.01)

        body = replay.json_body
        assert body is not None, (
            f"rehydrated session produced no response; status={replay.status} messages={replay.messages!r}"
        )
        # The bug: an uninitialized rehydrated session answers with
        # error -32602 "Received request before initialization was complete".
        assert "error" not in body, f"rehydrated session was not initialized: {body.get('error')!r}"
        assert "result" in body, body
