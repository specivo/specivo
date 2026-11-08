"""Regression tests: Streamable HTTP sessions across idle gaps, dead streams and expiry.

A client that goes quiet (laptop asleep, long think) must find its session
still working when it comes back, and a session that is genuinely gone must
be answered the way the MCP spec tells clients to recover from: HTTP 404,
after which the client opens a new session with ``initialize``. A JSON-RPC
``-32602`` "Invalid request parameters" for a stale session is the bug: it
reads as a caller mistake and no client recovers from it on its own.

The manager runs the production ``mcp._lowlevel_server`` with SSE responses
(``json_response=False``, as mounted), over the ASGI harness so streams can
be dropped abruptly or left half-open.
"""

from __future__ import annotations

from typing import Any

import anyio
import pytest

from specivo.mcp.persistent_session_manager import PersistentStreamableHTTPSessionManager
from specivo.mcp.server import mcp
from specivo.mcp.session_store import RedisSessionStore
from tests.integration.mcp_asgi_harness import (
    INIT_PARAMS,
    Capture,
    OpenStream,
    body_receive,
    http_scope,
    wait_for,
)
from tests.integration.test_mcp_session_store import _FakeRedis

pytestmark = [pytest.mark.integration, pytest.mark.serial, pytest.mark.asyncio(loop_scope="function")]

_KEY = "spv_session_expiry_test_key"
_INVALID_PARAMS = -32602


async def _resolve_owner(raw_key: str) -> tuple[int, int] | None:
    return (11, 22) if raw_key == _KEY else None


def _manager(
    store: RedisSessionStore,
    *,
    idle_timeout: float | None = 60.0,
) -> PersistentStreamableHTTPSessionManager:
    async def provider() -> RedisSessionStore:
        return store

    return PersistentStreamableHTTPSessionManager(
        app=mcp._lowlevel_server,
        event_store=None,
        json_response=False,
        stateless=False,
        session_idle_timeout=idle_timeout,
        store_provider=provider,
        owner_resolver=_resolve_owner,
    )


async def _post(
    manager: PersistentStreamableHTTPSessionManager,
    body: dict[str, Any],
    session_id: str | None,
) -> Capture:
    cap = Capture()
    with anyio.fail_after(5):
        await manager.handle_request(
            http_scope("POST", raw_key=_KEY, session_id=session_id),
            body_receive(body),
            cap,
        )
    return cap


async def _open_session(manager: PersistentStreamableHTTPSessionManager) -> str:
    init = await _post(manager, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": INIT_PARAMS}, None)
    assert init.status == 200, init.raw_body
    session_id = init.session_id
    assert session_id is not None
    done = await _post(manager, {"jsonrpc": "2.0", "method": "notifications/initialized"}, session_id)
    assert done.status == 202
    return session_id


async def _tools_list(manager: PersistentStreamableHTTPSessionManager, session_id: str, request_id: int) -> Capture:
    return await _post(manager, {"jsonrpc": "2.0", "id": request_id, "method": "tools/list"}, session_id)


def _assert_tools_listed(cap: Capture, request_id: int) -> None:
    assert cap.status == 200, (cap.status, cap.raw_body)
    reply = cap.reply(request_id)
    assert reply is not None, cap.raw_body
    assert "error" not in reply, reply
    assert reply["result"]["tools"], reply


def _assert_session_gone(cap: Capture) -> None:
    """The spec's answer for an unknown session: HTTP 404, never -32602."""
    assert cap.status == 404, (cap.status, cap.raw_body)
    for msg in cap.json_messages():
        assert msg.get("error", {}).get("code") != _INVALID_PARAMS, msg


async def _start_get(
    manager: PersistentStreamableHTTPSessionManager,
    session_id: str,
    stream: OpenStream,
    tg: anyio.abc.TaskGroup,
) -> None:
    async def run() -> None:
        await manager.handle_request(
            http_scope("GET", raw_key=_KEY, session_id=session_id, accept=b"text/event-stream"),
            stream.receive,
            stream.capture,
        )

    tg.start_soon(run)
    with anyio.fail_after(3):
        await stream.capture.started.wait()


# ---------------------------------------------------------------------------
# Idle gaps
# ---------------------------------------------------------------------------


async def test_session_reaped_from_memory_comes_back_after_idle_gap() -> None:
    """The in-memory transport is reaped while idle; the next call still works."""
    store = RedisSessionStore(_FakeRedis(), ttl_seconds=3600)
    manager = _manager(store, idle_timeout=0.2)
    async with manager.run():
        session_id = await _open_session(manager)
        _assert_tools_listed(await _tools_list(manager, session_id, 2), 2)

        await wait_for(lambda: session_id not in manager._server_instances)

        _assert_tools_listed(await _tools_list(manager, session_id, 3), 3)
        assert session_id in manager._server_instances


async def test_session_in_active_use_never_expires() -> None:
    """Steady use keeps both the live transport and the persisted entry alive."""
    redis = _FakeRedis()
    store = RedisSessionStore(redis, ttl_seconds=100)
    manager = _manager(store, idle_timeout=2.0)
    async with manager.run():
        session_id = await _open_session(manager)
        transport = manager._server_instances[session_id]

        # Calls every 0.5 s for well past the 2 s idle window (with room for
        # a slow, loaded test host), while the persisted entry's clock moves
        # 60 s per call against a 100 s TTL.
        for request_id in range(2, 10):
            await anyio.sleep(0.5)
            redis._advance(60)
            _assert_tools_listed(await _tools_list(manager, session_id, request_id), request_id)
            # The touch runs in the background; let it land.
            await wait_for(lambda: redis._expiries.get(f"mcp:session:{session_id}") == redis._now + 100)

        assert manager._server_instances.get(session_id) is transport
        assert await store.peek(session_id) is not None


async def test_expired_session_answers_404_not_invalid_params() -> None:
    """Past the persisted TTL the session is gone: 404 so the client re-initializes."""
    redis = _FakeRedis()
    store = RedisSessionStore(redis, ttl_seconds=100)
    manager = _manager(store, idle_timeout=0.2)
    async with manager.run():
        session_id = await _open_session(manager)
        await wait_for(lambda: session_id not in manager._server_instances)
        redis._advance(101)

        _assert_session_gone(await _tools_list(manager, session_id, 2))

        # And the client's recovery works: a fresh initialize opens a new session.
        new_id = await _open_session(manager)
        assert new_id != session_id
        _assert_tools_listed(await _tools_list(manager, new_id, 3), 3)


async def test_unknown_session_answers_404_not_invalid_params() -> None:
    store = RedisSessionStore(_FakeRedis(), ttl_seconds=100)
    manager = _manager(store)
    async with manager.run():
        _assert_session_gone(await _tools_list(manager, "0" * 32, 2))
        call = await _post(
            manager,
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "specivo_whoami", "arguments": {}}},
            "f" * 32,
        )
        _assert_session_gone(call)


async def test_terminated_but_not_yet_discarded_session_is_rehydrated() -> None:
    """A transport caught between idle cancel and discard must not answer 404."""
    store = RedisSessionStore(_FakeRedis(), ttl_seconds=3600)
    manager = _manager(store)
    async with manager.run():
        session_id = await _open_session(manager)
        transport = manager._server_instances[session_id]
        # Simulate the idle reaper having fired: the transport is terminated
        # but the manager has not yet forgotten it.
        await transport.terminate()
        assert manager._server_instances.get(session_id) is transport

        _assert_tools_listed(await _tools_list(manager, session_id, 2), 2)
        assert manager._server_instances[session_id] is not transport


# ---------------------------------------------------------------------------
# The client machine sleeps: dead or half-open GET streams
# ---------------------------------------------------------------------------


async def test_abruptly_dropped_get_stream_keeps_the_session() -> None:
    """A GET stream cut with no close does not end or poison the session."""
    store = RedisSessionStore(_FakeRedis(), ttl_seconds=3600)
    manager = _manager(store, idle_timeout=0.3)
    async with manager.run(), anyio.create_task_group() as tg:
        session_id = await _open_session(manager)
        stream = OpenStream()
        await _start_get(manager, session_id, stream, tg)

        stream.hang_up()
        # Past any short timer: the in-memory transport may be reaped, the
        # session must not be.
        await anyio.sleep(0.5)

        _assert_tools_listed(await _tools_list(manager, session_id, 2), 2)
        tg.cancel_scope.cancel()


async def test_half_open_get_stream_does_not_block_the_reconnect() -> None:
    """After a sleep the old GET is still 'open' server-side; a new GET replaces it."""
    store = RedisSessionStore(_FakeRedis(), ttl_seconds=3600)
    manager = _manager(store)
    async with manager.run(), anyio.create_task_group() as tg:
        session_id = await _open_session(manager)
        stale = OpenStream()  # never hangs up: the server cannot tell it died
        await _start_get(manager, session_id, stale, tg)

        fresh = OpenStream()
        await _start_get(manager, session_id, fresh, tg)
        assert fresh.capture.status == 200, fresh.capture.raw_body

        # The stale stream was closed by the server to make room.
        await wait_for(lambda: any(m.get("more_body") is False for m in stale.capture.messages))

        _assert_tools_listed(await _tools_list(manager, session_id, 2), 2)
        tg.cancel_scope.cancel()


async def test_session_survives_manager_restart_after_dead_stream() -> None:
    """Sleep through an api restart: the old session id still works on the new process."""
    redis = _FakeRedis()
    manager_a = _manager(RedisSessionStore(redis, ttl_seconds=3600))
    async with manager_a.run(), anyio.create_task_group() as tg:
        session_id = await _open_session(manager_a)
        stream = OpenStream()
        await _start_get(manager_a, session_id, stream, tg)
        tg.cancel_scope.cancel()  # the process dies under the open stream

    manager_b = _manager(RedisSessionStore(redis, ttl_seconds=3600))
    async with manager_b.run():
        _assert_tools_listed(await _tools_list(manager_b, session_id, 2), 2)


async def test_deleted_session_answers_404_and_is_not_rehydrated() -> None:
    """A session the client ended with DELETE stays ended."""
    redis = _FakeRedis()
    store = RedisSessionStore(redis, ttl_seconds=3600)
    manager = _manager(store)
    async with manager.run():
        session_id = await _open_session(manager)
        deleted = Capture()
        await manager.handle_request(
            http_scope("DELETE", raw_key=_KEY, session_id=session_id), body_receive(None), deleted
        )
        assert deleted.status == 200, deleted.raw_body
        assert await store.peek(session_id) is None

        _assert_session_gone(await _tools_list(manager, session_id, 2))


# ---------------------------------------------------------------------------
# Opening requests that do not open a session
# ---------------------------------------------------------------------------


async def test_failed_opening_request_leaves_no_session_behind() -> None:
    """A non-initialize request without a session id is refused and not kept."""
    redis = _FakeRedis()
    store = RedisSessionStore(redis, ttl_seconds=3600)
    manager = _manager(store)
    async with manager.run():
        cap = await _tools_list(manager, None, 1)  # type: ignore[arg-type]
        assert cap.status == 400, cap.raw_body
        assert manager._server_instances == {}
        assert not [k for k in redis._hashes if k.startswith("mcp:session:")]
