"""Toolsets chosen in the MCP URL, over the mounted transports.

Streamable HTTP clients send the ``toolsets`` query parameter on every request;
a legacy SSE client names it once, on the stream's ``GET``, and the message
endpoint the server announces carries it on.
"""

from __future__ import annotations

import json
from typing import Any

import anyio
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from mcp.server.auth.provider import AccessToken

from specivo.core.constants import MCP_PENDING_CLIENT_ID
from specivo.mcp import mount
from specivo.mcp.auth import SpvTokenVerifier, key_principal
from specivo.mcp.toolsets import CORE, PLANNING, TOOLSETS
from tests.integration.mcp_asgi_harness import Capture, OpenStream, wait_for
from tests.integration.test_mcp_sse_reconnect import _KEY, _handshake, _scope, _SseClient
from tests.integration.test_mcp_transport import _INIT_PARAMS, mcp_key  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.serial, pytest.mark.usefixtures("tool_engine")]

_ACCEPT = "application/json, text/event-stream"


@pytest.fixture
def sse_app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """The mounted MCP app with the API key check stubbed (no database needed for SSE listing)."""

    async def verify_token(self: SpvTokenVerifier, token: str) -> AccessToken | None:
        if token != _KEY:
            return None
        return AccessToken(token=token, client_id=MCP_PENDING_CLIENT_ID, scopes=["mcp"], subject=key_principal(token))

    monkeypatch.setattr(SpvTokenVerifier, "verify_token", verify_token)
    monkeypatch.setattr(mount, "_session_manager", None)
    application = FastAPI()
    mount.mount_mcp(application)
    return application


def _sse_json(text: str) -> dict[str, Any]:
    for line in text.splitlines():
        if line.startswith("data:"):
            payload = line.removeprefix("data:").strip()
            if payload:
                return json.loads(payload)
    return json.loads(text)


async def _http_session(client: AsyncClient, url: str, raw_key: str) -> tuple[dict[str, str], dict[str, Any]]:
    """Initialize over Streamable HTTP at *url*; return the session headers and the reply."""
    headers = {"Authorization": f"Bearer {raw_key}", "Accept": _ACCEPT}
    init = await client.post(
        url, headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": _INIT_PARAMS}
    )
    assert init.status_code == 200, init.text
    reply = _sse_json(init.text)
    if "error" in reply:
        return headers, reply
    headers = {**headers, "Mcp-Session-Id": init.headers["mcp-session-id"], "Mcp-Protocol-Version": "2025-03-26"}
    done = await client.post(url, headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert done.status_code == 202
    return headers, reply


async def _tool_names(client: AsyncClient, url: str, headers: dict[str, str]) -> set[str]:
    listed = await client.post(url, headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    return {tool["name"] for tool in _sse_json(listed.text)["result"]["tools"]}


@pytest.mark.asyncio(loop_scope="function")
async def test_streamable_http_lists_the_toolsets_of_its_url(mcp_key) -> None:  # noqa: F811
    from specivo.main import create_app
    from specivo.mcp.mount import get_mcp_session_manager

    raw_key, _user_id, _key_id = mcp_key
    app = create_app()
    manager = get_mcp_session_manager()
    assert manager is not None
    async with manager.run(), AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers, _ = await _http_session(client, "/mcp/", raw_key)
        assert await _tool_names(client, "/mcp/", headers) == set(TOOLSETS[CORE])

        called = await client.post(
            "/mcp/",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "specivo_list_sprint_issues", "arguments": {"project_key": "X", "sprint_id": 1}},
            },
        )
        result = _sse_json(called.text)["result"]
        assert result["isError"] is True
        assert "'planning' toolset" in result["content"][0]["text"]
        assert "?toolsets=core,planning" in result["content"][0]["text"]

        url = "/mcp/?toolsets=core,planning"
        headers, _ = await _http_session(client, url, raw_key)
        assert await _tool_names(client, url, headers) == set(TOOLSETS[CORE]) | set(TOOLSETS[PLANNING])

        url = "/mcp/?toolsets=all"
        headers, _ = await _http_session(client, url, raw_key)
        assert len(await _tool_names(client, url, headers)) == sum(len(tools) for tools in TOOLSETS.values())


@pytest.mark.asyncio(loop_scope="function")
async def test_streamable_http_refuses_an_unknown_toolset(mcp_key) -> None:  # noqa: F811
    from specivo.main import create_app
    from specivo.mcp.mount import get_mcp_session_manager

    raw_key, _user_id, _key_id = mcp_key
    app = create_app()
    manager = get_mcp_session_manager()
    assert manager is not None
    async with manager.run(), AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _, reply = await _http_session(client, "/mcp/?toolsets=core,sprints", raw_key)
    assert reply["error"]["code"] == -32602
    assert "Unknown MCP toolset 'sprints'" in reply["error"]["message"]
    assert "planning" in reply["error"]["message"]


@pytest.mark.asyncio(loop_scope="function")
async def test_an_sse_stream_keeps_the_toolsets_of_its_url(sse_app: FastAPI) -> None:
    async with anyio.create_task_group() as tg:
        client = _SseClient(sse_app, tg)
        # Connect with the toolsets in the stream's URL.
        scope = _scope("GET", "/mcp/sse/", b"toolsets=planning")
        tg.start_soon(sse_app, scope, client.stream.receive, client.stream.capture)
        await wait_for(lambda: any(name == "endpoint" for name, _ in client.stream.capture.events()))
        client.endpoint = next(data for name, data in client.stream.capture.events() if name == "endpoint")
        # The announced message endpoint carries the stream's toolsets.
        assert "?toolsets=planning&session_id=" in client.endpoint
        await _handshake(client)
        reply = await client.call({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert {tool["name"] for tool in reply["result"]["tools"]} == set(TOOLSETS[PLANNING])

        default = _SseClient(sse_app, tg)
        await default.connect()
        await _handshake(default)
        reply = await default.call({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert {tool["name"] for tool in reply["result"]["tools"]} == set(TOOLSETS[CORE])
        tg.cancel_scope.cancel()


@pytest.mark.asyncio(loop_scope="function")
async def test_an_sse_stream_with_an_unknown_toolset_is_refused(sse_app: FastAPI) -> None:
    stream = OpenStream()
    with anyio.fail_after(5):
        await sse_app(_scope("GET", "/mcp/sse/", b"toolsets=bogus"), stream.receive, stream.capture)
    capture: Capture = stream.capture
    assert capture.status == 400
    body = json.loads(capture.raw_body)
    assert body["errors"][0]["code"] == "unknown_toolset"
    assert "Unknown MCP toolset 'bogus'" in body["errors"][0]["message"]
