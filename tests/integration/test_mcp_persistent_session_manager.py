"""Regression tests for :mod:`specivo.mcp.persistent_session_manager`.

The critical test here — ``test_session_survives_manager_restart`` — proves
the user-visible fix for the "HTTP 404: Could not find session" bug that
previously forced MCP clients to manually reconnect after every api
restart.

The test strategy is deliberately narrow: we drive the subclassed session
manager's dispatch method directly via a minimal ASGI stub and a fake
transport, so we avoid standing up the entire MCP ``Server`` event loop.
That keeps the test focused on the exact branch the fix introduces — the
"Redis knows this session id, mint a fresh transport under that id"
rehydration path — and keeps it fast enough to run under ``pytest -q``
without extra infrastructure.
"""

from __future__ import annotations

from typing import Any

import anyio
import pytest

from specivo.mcp.persistent_session_manager import (
    PersistentStreamableHTTPSessionManager,
)
from specivo.mcp.session_store import RedisSessionStore
from tests.integration.test_mcp_session_store import _FakeRedis

pytestmark = pytest.mark.asyncio(loop_scope="function")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeHTTPTransport:
    """Stand-in for ``StreamableHTTPServerTransport`` used by the SDK.

    Records the requests routed to it and exposes the attributes the
    manager touches in its cleanup path.
    """

    def __init__(self, *, mcp_session_id: str, **_: Any) -> None:
        self.mcp_session_id = mcp_session_id
        self.is_terminated = False
        self.handled: list[dict[str, Any]] = []
        self._connected = False

    async def handle_request(self, scope: Any, receive: Any, send: Any) -> None:
        self.handled.append({"scope": scope, "receive": receive, "send": send})
        # Minimal ASGI response so the caller's ``send`` is exercised.
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    class _NoopCtx:
        async def __aenter__(self) -> tuple[Any, Any]:
            return (object(), object())

        async def __aexit__(self, *exc: Any) -> None:
            return None

    def connect(self) -> _FakeHTTPTransport._NoopCtx:
        return self._NoopCtx()

    async def terminate(self) -> None:
        self.is_terminated = True


class _FakeLowLevelApp:
    """Replaces ``mcp.server.lowlevel.Server`` for tests.

    Its ``run()`` simply sleeps forever (until the manager's task group is
    cancelled). We never exercise real protocol behaviour here.
    """

    def create_initialization_options(self) -> dict[str, Any]:
        return {}

    async def run(self, *_args: Any, **_kwargs: Any) -> None:
        # Block until the manager cancels the task group.
        await anyio.sleep_forever()


# ---------------------------------------------------------------------------
# ASGI scope helpers
# ---------------------------------------------------------------------------


def _make_scope(session_id: str | None = None) -> dict[str, Any]:
    headers: list[tuple[bytes, bytes]] = [(b"user-agent", b"pytest-mcp")]
    if session_id is not None:
        headers.append((b"mcp-session-id", session_id.encode("ascii")))
    return {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": headers,
        "query_string": b"",
    }


class _Sender:
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
    def body(self) -> bytes:
        return b"".join(
            m.get("body", b"") for m in self.messages if m["type"] == "http.response.body"
        )


async def _noop_receive() -> dict[str, Any]:  # pragma: no cover - never awaited
    return {"type": "http.request", "body": b"", "more_body": False}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def patch_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the real StreamableHTTPServerTransport the manager creates."""
    import specivo.mcp.persistent_session_manager as mod

    monkeypatch.setattr(mod, "StreamableHTTPServerTransport", _FakeHTTPTransport)


@pytest.fixture
def fake_redis() -> _FakeRedis:
    return _FakeRedis()


@pytest.fixture
def store(fake_redis: _FakeRedis) -> RedisSessionStore:
    return RedisSessionStore(fake_redis, ttl_seconds=300)


def _make_manager(
    store: RedisSessionStore | None,
) -> PersistentStreamableHTTPSessionManager:
    async def provider() -> RedisSessionStore | None:
        return store

    return PersistentStreamableHTTPSessionManager(
        app=_FakeLowLevelApp(),
        event_store=None,
        json_response=False,
        stateless=False,
        store_provider=provider,
    )


# ---------------------------------------------------------------------------
# Construction guardrails
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_rejects_stateless_true() -> None:
    with pytest.raises(ValueError):
        PersistentStreamableHTTPSessionManager(
            app=_FakeLowLevelApp(),
            stateless=True,
        )


# ---------------------------------------------------------------------------
# Brand-new session creation writes to Redis
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_new_session_is_persisted_to_redis(
    patch_transport: None,
    store: RedisSessionStore,
    fake_redis: _FakeRedis,
) -> None:
    manager = _make_manager(store)
    send = _Sender()

    async with manager.run():
        await manager._handle_stateful_request(_make_scope(None), _noop_receive, send)

        # One transport should now exist, and its id should be in Redis.
        assert len(manager._server_instances) == 1
        session_id = next(iter(manager._server_instances))
        entry = await store.peek(session_id)
        assert entry is not None
        assert entry["transport"] == "streamable_http"
        assert entry["user_agent"] == "pytest-mcp"

    assert send.status == 200


# ---------------------------------------------------------------------------
# Unknown session id with no Redis entry still 404s
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_unknown_session_id_without_redis_entry_returns_404(
    patch_transport: None,
    store: RedisSessionStore,
) -> None:
    manager = _make_manager(store)
    send = _Sender()

    async with manager.run():
        await manager._handle_stateful_request(
            _make_scope("never-seen"), _noop_receive, send
        )

    assert send.status == 404
    assert b"Session not found" in send.body


# ---------------------------------------------------------------------------
# THE critical regression test: restart survival
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_session_survives_manager_restart(
    patch_transport: None,
    fake_redis: _FakeRedis,
) -> None:
    """Client session id keeps working after the session manager is replaced.

    This is the literal fix for SPECIVO-147: before this change,
    restarting the api dropped every live MCP session and clients had to
    manually reconnect. The fix persists session ids in Redis and rehydrates
    a fresh transport on demand when a previously known id comes back.
    """
    store_a = RedisSessionStore(fake_redis, ttl_seconds=300)
    store_b = RedisSessionStore(fake_redis, ttl_seconds=300)

    # ---- Phase 1: old manager instance, new session created -------------
    manager_a = _make_manager(store_a)
    send_a = _Sender()
    async with manager_a.run():
        await manager_a._handle_stateful_request(
            _make_scope(None), _noop_receive, send_a
        )
        assert send_a.status == 200
        session_id = next(iter(manager_a._server_instances))
    # Exiting the run() context simulates the api process dying: task group
    # cancelled, _server_instances cleared.
    assert manager_a._server_instances == {}

    # Redis still has the session.
    assert await store_b.peek(session_id) is not None

    # ---- Phase 2: fresh manager, client reuses the old session id -------
    manager_b = _make_manager(store_b)
    send_b = _Sender()
    async with manager_b.run():
        await manager_b._handle_stateful_request(
            _make_scope(session_id), _noop_receive, send_b
        )

        # Critical assertion: no 404. The request succeeded and a fresh
        # transport was registered under the exact same session id the
        # client supplied.
        assert send_b.status == 200, (
            f"Rehydration failed: got status {send_b.status} body={send_b.body!r}"
        )
        assert session_id in manager_b._server_instances
        rehydrated = manager_b._server_instances[session_id]
        assert isinstance(rehydrated, _FakeHTTPTransport)
        assert rehydrated.mcp_session_id == session_id
        assert len(rehydrated.handled) == 1


# ---------------------------------------------------------------------------
# Redis down should fail open: request still 404s, server does not crash
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_redis_down_during_rehydration_falls_back_to_404(
    patch_transport: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _BrokenStore:
        async def get(self, _sid: str) -> dict[str, str] | None:
            raise RuntimeError("redis is down")

        async def create(self, *_a: Any, **_kw: Any) -> None:  # pragma: no cover
            raise RuntimeError("redis is down")

        async def touch(self, *_a: Any, **_kw: Any) -> bool:  # pragma: no cover
            raise RuntimeError("redis is down")

    async def provider() -> Any:
        return _BrokenStore()

    manager = PersistentStreamableHTTPSessionManager(
        app=_FakeLowLevelApp(),
        event_store=None,
        json_response=False,
        stateless=False,
        store_provider=provider,
    )
    send = _Sender()

    async with manager.run():
        await manager._handle_stateful_request(
            _make_scope("ghost"), _noop_receive, send
        )

    assert send.status == 404


# ---------------------------------------------------------------------------
# TTL refresh on every successful rehydration
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_rehydration_refreshes_sliding_ttl(
    patch_transport: None,
    fake_redis: _FakeRedis,
) -> None:
    store = RedisSessionStore(fake_redis, ttl_seconds=300)

    # Seed the store directly, as though an earlier process had done it.
    await store.create("seeded", {"api_key_id": 1, "user_id": 2})
    original_exp = fake_redis._expiries["mcp:session:seeded"]

    fake_redis._advance(60)  # burn some of the TTL

    manager = _make_manager(store)
    send = _Sender()
    async with manager.run():
        await manager._handle_stateful_request(
            _make_scope("seeded"), _noop_receive, send
        )

    assert send.status == 200
    new_exp = fake_redis._expiries["mcp:session:seeded"]
    assert new_exp > original_exp


# ---------------------------------------------------------------------------
# A long-lived request must not block other sessions
# ---------------------------------------------------------------------------


class _BlockingHTTPTransport(_FakeHTTPTransport):
    """Transport whose ``handle_request`` never returns.

    Models a GET that opens an SSE stream: it stays open for as long as the
    client is connected, which can be hours.
    """

    started = anyio.Event()

    async def handle_request(self, scope: Any, receive: Any, send: Any) -> None:
        type(self).started.set()
        await anyio.sleep_forever()


@pytest.mark.unit
async def test_long_lived_request_does_not_block_other_sessions(
    monkeypatch: pytest.MonkeyPatch,
    fake_redis: _FakeRedis,
) -> None:
    """One client streaming must not stall session creation for everyone else.

    Serving the request while holding the session-creation lock meant the first
    client to reconnect after a restart — opening an SSE stream that never ends
    — locked out every other session indefinitely: new sessions and rehydrations
    alike hung with no error and no log line.
    """
    import specivo.mcp.persistent_session_manager as mod

    store = RedisSessionStore(fake_redis, ttl_seconds=300)
    await store.create("streamer", {"api_key_id": 1, "user_id": 2})

    monkeypatch.setattr(mod, "StreamableHTTPServerTransport", _BlockingHTTPTransport)
    manager = _make_manager(store)

    async with manager.run():
        async with anyio.create_task_group() as tg:
            # Client A rehydrates its session and starts streaming forever.
            tg.start_soon(
                manager._handle_stateful_request,
                _make_scope("streamer"),
                _noop_receive,
                _Sender(),
            )
            await _BlockingHTTPTransport.started.wait()

            # Client B must still get a session while A is mid-stream.
            monkeypatch.setattr(mod, "StreamableHTTPServerTransport", _FakeHTTPTransport)
            send_b = _Sender()
            with anyio.fail_after(5):
                await manager._handle_stateful_request(_make_scope(None), _noop_receive, send_b)

            assert send_b.status == 200
            assert len(manager._server_instances) == 2
            tg.cancel_scope.cancel()
