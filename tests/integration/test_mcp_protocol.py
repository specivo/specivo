"""End-to-end MCP protocol tests through the mounted Streamable HTTP app.

These drive the real SDK stack (auth middleware, persistent session
manager, ``MCPServer`` dispatch) over ASGI, so they catch SDK API changes
that the direct tool-function tests in ``test_mcp_server.py`` cannot see.

Also covers how tool failures reach the client: user-facing errors keep
their message, unexpected ones are reported generically so internal
details never leak.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from mcp import Client

from specivo.core.exceptions import NotFoundError
from specivo.mcp import server as mcp_server_module
from specivo.mcp.server import mcp
from tests.integration.test_mcp_transport import _INIT_PARAMS, mcp_key  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.serial]

_ACCEPT = "application/json, text/event-stream"


def _sse_json(text: str) -> dict[str, Any]:
    """Return the JSON-RPC message carried by an SSE response body."""
    for line in text.splitlines():
        if line.startswith("data:"):
            payload = line.removeprefix("data:").strip()
            if payload:
                return json.loads(payload)
    return json.loads(text)


@pytest.mark.asyncio(loop_scope="function")
async def test_initialize_list_call_and_read_over_http(mcp_key) -> None:  # noqa: F811
    from specivo.main import create_app
    from specivo.mcp.mount import get_mcp_session_manager

    raw_key, _user_id, _key_id = mcp_key
    app = create_app()
    manager = get_mcp_session_manager()
    assert manager is not None

    headers = {"Authorization": f"Bearer {raw_key}", "Accept": _ACCEPT}
    async with manager.run():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            init = await client.post(
                "/mcp/",
                headers=headers,
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": _INIT_PARAMS},
            )
            assert init.status_code == 200, init.text
            session_id = init.headers["mcp-session-id"]
            init_body = _sse_json(init.text)
            assert init_body["result"]["serverInfo"]["name"] == "specivo"
            assert "specivo_setup_guide" in init_body["result"]["instructions"]

            headers = {**headers, "Mcp-Session-Id": session_id, "Mcp-Protocol-Version": "2025-03-26"}
            notified = await client.post(
                "/mcp/",
                headers=headers,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            assert notified.status_code == 202

            listed = await client.post(
                "/mcp/",
                headers=headers,
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            )
            tools = {t["name"]: t for t in _sse_json(listed.text)["result"]["tools"]}
            assert "specivo_whoami" in tools
            assert "inputSchema" in tools["specivo_list_issues"]
            assert tools["specivo_whoami"]["outputSchema"]["required"] == ["result"]

            called = await client.post(
                "/mcp/",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "specivo_whoami", "arguments": {}},
                },
            )
            result = _sse_json(called.text)["result"]
            assert result["isError"] is False
            assert "mcp_test_user" in result["content"][0]["text"]

            read = await client.post(
                "/mcp/",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "resources/read",
                    "params": {"uri": "specivo://docs/agent-setup"},
                },
            )
            contents = _sse_json(read.text)["result"]["contents"]
            assert contents[0]["mimeType"] == "text/markdown"
            assert "specivo_whoami" in contents[0]["text"]


@pytest.mark.asyncio(loop_scope="function")
async def test_unexpected_tool_error_does_not_leak_details(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "password=hunter2 at /srv/specivo/secrets.env"

    async def boom(_session: Any) -> Any:
        raise RuntimeError(secret)

    monkeypatch.setattr(mcp_server_module, "authenticate_mcp_tool", boom)

    async with Client(mcp) as client:
        result = await client.call_tool("specivo_whoami", {})

    assert result.is_error is True
    text = result.content[0].text  # type: ignore[union-attr]
    assert "Internal error while executing tool" in text
    assert "hunter2" not in text
    assert "/srv/specivo" not in text


@pytest.mark.asyncio(loop_scope="function")
async def test_app_error_message_reaches_client(monkeypatch: pytest.MonkeyPatch) -> None:
    async def not_found(_session: Any) -> Any:
        raise NotFoundError("Wiki page 'missing-page' not found")

    monkeypatch.setattr(mcp_server_module, "authenticate_mcp_tool", not_found)

    async with Client(mcp) as client:
        result = await client.call_tool("specivo_whoami", {})

    assert result.is_error is True
    text = result.content[0].text  # type: ignore[union-attr]
    assert text == "Error executing tool specivo_whoami: Wiki page 'missing-page' not found"


# ---------------------------------------------------------------------------
# Session owner resolution (gate for persisting and rehydrating sessions)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
async def test_resolve_key_owner_returns_key_and_user(mcp_key, db_engine) -> None:  # noqa: F811
    from specivo.core.database import set_engine
    from specivo.mcp.auth import resolve_key_owner

    set_engine(db_engine)  # the resolver opens its own session on this loop's engine

    raw_key, user_id, key_id = mcp_key
    assert await resolve_key_owner(raw_key) == (key_id, user_id)
    assert await resolve_key_owner("spv_" + "x" * 40) is None


@pytest.mark.asyncio(loop_scope="function")
@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE api_keys SET is_active = false WHERE id = :key_id",
        "UPDATE users SET status = 'deactivated' WHERE id = :user_id",
    ],
    ids=["revoked-key", "deactivated-user"],
)
async def test_resolve_key_owner_refuses_unusable_key(mcp_key, db_engine, statement: str) -> None:  # noqa: F811
    from sqlalchemy import text

    from specivo.core.database import set_engine
    from specivo.mcp.auth import resolve_key_owner

    set_engine(db_engine)
    raw_key, user_id, key_id = mcp_key
    async with db_engine.begin() as conn:
        await conn.execute(text(statement), {"key_id": key_id, "user_id": user_id})

    assert await resolve_key_owner(raw_key) is None
