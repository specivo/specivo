"""Regression tests: the legacy SSE transport after a client stream reconnect.

The SDK's SSE transport mints a new session id on every GET. A client whose
event stream drops (sleep, network blip) reconnects, adopts the new message
endpoint the server announces, and carries on with its next request: its
protocol layer still considers itself initialized, so it never repeats
``initialize``. Each SSE stream used to start an uninitialized session, so
every request after a reconnect was refused with JSON-RPC ``-32602``
"Invalid request parameters" until the user reconnected by hand.

These tests drive the SSE app exactly as mounted by ``mount_mcp``; only the
API key check is stubbed.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

import anyio
import pytest
from fastapi import FastAPI
from mcp.server.auth.provider import AccessToken

from specivo.core.constants import MCP_PENDING_CLIENT_ID
from specivo.mcp import mount
from specivo.mcp.auth import SpvTokenVerifier, key_principal
from tests.integration.mcp_asgi_harness import INIT_PARAMS, Capture, OpenStream, wait_for

pytestmark = [pytest.mark.integration, pytest.mark.serial, pytest.mark.asyncio(loop_scope="function")]

_KEY = "spv_sse_reconnect_test_key"
_INVALID_PARAMS = -32602


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    async def verify_token(self: SpvTokenVerifier, token: str) -> AccessToken | None:
        if token != _KEY:
            return None
        return AccessToken(token=token, client_id=MCP_PENDING_CLIENT_ID, scopes=["mcp"], subject=key_principal(token))

    monkeypatch.setattr(SpvTokenVerifier, "verify_token", verify_token)
    monkeypatch.setattr(mount, "_session_manager", None)
    application = FastAPI()
    mount.mount_mcp(application)
    return application


def _scope(method: str, path: str, query: bytes = b"") -> dict[str, Any]:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 50000),
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": query,
        "headers": [
            (b"host", b"testserver"),
            (b"authorization", f"Bearer {_KEY}".encode()),
            (b"content-type", b"application/json"),
            (b"accept", b"text/event-stream" if method == "GET" else b"application/json"),
        ],
    }


class _SseClient:
    """One SSE event stream plus the message endpoint it announced."""

    def __init__(self, app: FastAPI, tg: anyio.abc.TaskGroup) -> None:
        self.app = app
        self.tg = tg
        self.stream = OpenStream()
        self.endpoint: str = ""

    async def connect(self) -> None:
        self.tg.start_soon(self.app, _scope("GET", "/mcp/sse/"), self.stream.receive, self.stream.capture)
        await wait_for(lambda: any(name == "endpoint" for name, _ in self.stream.capture.events()))
        self.endpoint = next(data for name, data in self.stream.capture.events() if name == "endpoint")

    async def post(self, body: dict[str, Any]) -> Capture:
        parts = urlsplit(self.endpoint)
        raw = json.dumps(body).encode()
        delivered = False

        async def receive() -> dict[str, Any]:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": raw, "more_body": False}
            await anyio.sleep_forever()
            raise AssertionError("unreachable")

        cap = Capture()
        with anyio.fail_after(5):
            await self.app(_scope("POST", parts.path, parts.query.encode()), receive, cap)
        return cap

    async def call(self, body: dict[str, Any]) -> dict[str, Any]:
        """POST a request and wait for its reply on the event stream."""
        accepted = await self.post(body)
        assert accepted.status == 202, (accepted.status, accepted.raw_body)
        await wait_for(lambda: self.stream.capture.reply(body["id"]) is not None)
        reply = self.stream.capture.reply(body["id"])
        assert reply is not None
        return reply


async def _handshake(client: _SseClient) -> None:
    reply = await client.call({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": INIT_PARAMS})
    assert reply["result"]["serverInfo"], reply
    done = await client.post({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert done.status == 202


def _assert_tools_listed(reply: dict[str, Any]) -> None:
    assert reply.get("error", {}).get("code") != _INVALID_PARAMS, reply
    assert "error" not in reply, reply
    assert reply["result"]["tools"], reply


async def test_calls_after_a_stream_reconnect_work_without_a_new_handshake(app: FastAPI) -> None:
    async with anyio.create_task_group() as tg:
        first = _SseClient(app, tg)
        await first.connect()
        await _handshake(first)
        _assert_tools_listed(await first.call({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}))

        # The client's machine sleeps: the stream drops with no close.
        first.stream.hang_up()

        # It wakes, reconnects and adopts the new endpoint, but its protocol
        # layer is already initialized, so it sends no second initialize.
        second = _SseClient(app, tg)
        await second.connect()
        assert second.endpoint != first.endpoint
        _assert_tools_listed(await second.call({"jsonrpc": "2.0", "id": 3, "method": "tools/list"}))
        whoami = await second.call(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "specivo_whoami", "arguments": {}},
            }
        )
        assert whoami.get("error", {}).get("code") != _INVALID_PARAMS, whoami

        tg.cancel_scope.cancel()


async def test_post_to_a_dropped_stream_answers_404_not_invalid_params(app: FastAPI) -> None:
    async with anyio.create_task_group() as tg:
        first = _SseClient(app, tg)
        await first.connect()
        await _handshake(first)
        first.stream.hang_up()

        # Once the server has noticed the disconnect, the old endpoint is gone.
        stale = Capture()
        with anyio.fail_after(3):
            while stale.status != 404:
                await anyio.sleep(0.02)
                stale = await first.post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert b"-32602" not in stale.raw_body
        assert b"Could not find session" in stale.raw_body

        tg.cancel_scope.cancel()


async def test_a_fresh_client_still_gets_a_normal_handshake(app: FastAPI) -> None:
    async with anyio.create_task_group() as tg:
        client = _SseClient(app, tg)
        await client.connect()
        reply = await client.call({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": INIT_PARAMS})
        result = reply["result"]
        assert result["protocolVersion"] == INIT_PARAMS["protocolVersion"]
        assert result["serverInfo"]["name"]
        assert "tools" in result["capabilities"]
        tg.cancel_scope.cancel()
