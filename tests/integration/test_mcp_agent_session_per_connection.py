"""Over the real Streamable HTTP stack, each MCP connection is its own agent session.

Two connections opened with one API key (two terminal sessions of one
person, or a new client session started minutes after the last one) must not
share an agent session, or the issue one of them marks would be attributed
to the other's AI usage. A client that keeps its ``Mcp-Session-Id`` across an
api restart (the session is rebuilt from Redis) keeps its agent session.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from specivo.models.agent_session import AgentSession
from tests.integration.test_mcp_transport import _INIT_PARAMS, mcp_key  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.serial,
    pytest.mark.asyncio(loop_scope="function"),
    pytest.mark.usefixtures("tool_engine"),
]

_ACCEPT = "application/json, text/event-stream"


def _sse_json(text: str) -> dict[str, Any]:
    for line in text.splitlines():
        if line.startswith("data:"):
            payload = line.removeprefix("data:").strip()
            if payload:
                return json.loads(payload)
    return json.loads(text)


async def _connect(client: AsyncClient, raw_key: str) -> dict[str, str]:
    """Open a new MCP connection; return the headers its later requests carry."""
    headers = {"Authorization": f"Bearer {raw_key}", "Accept": _ACCEPT}
    init = await client.post(
        "/mcp/",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": _INIT_PARAMS},
    )
    assert init.status_code == 200, init.text
    headers = {**headers, "Mcp-Session-Id": init.headers["mcp-session-id"], "Mcp-Protocol-Version": "2025-03-26"}
    await client.post("/mcp/", headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    return headers


async def _whoami(client: AsyncClient, headers: dict[str, str]) -> None:
    called = await client.post(
        "/mcp/",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "specivo_whoami", "arguments": {}},
        },
    )
    assert called.status_code == 200, called.text
    assert _sse_json(called.text)["result"]["isError"] is False


async def _session_ids(db_engine, key_id: int) -> list[int]:
    async with db_engine.connect() as conn:
        rows = await conn.scalars(select(AgentSession.id).where(AgentSession.api_key_id == key_id))
        return sorted(rows.all())


async def _cleanup(db_engine, key_id: int) -> None:
    async with db_engine.begin() as conn:
        await conn.execute(delete(AgentSession).where(AgentSession.api_key_id == key_id))


async def test_two_connections_of_one_key_get_two_agent_sessions(mcp_key, db_engine) -> None:  # noqa: F811
    from specivo.main import create_app
    from specivo.mcp.mount import get_mcp_session_manager

    raw_key, _user_id, key_id = mcp_key
    app = create_app()
    manager = get_mcp_session_manager()
    assert manager is not None
    try:
        async with manager.run():
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                first = await _connect(client, raw_key)
                await _whoami(client, first)
                await _whoami(client, first)
                assert len(await _session_ids(db_engine, key_id)) == 1

                second = await _connect(client, raw_key)
                await _whoami(client, second)
                assert len(await _session_ids(db_engine, key_id)) == 2

                # Going back to the first connection does not open a third.
                await _whoami(client, first)
                assert len(await _session_ids(db_engine, key_id)) == 2
    finally:
        await _cleanup(db_engine, key_id)


async def test_a_session_restored_after_a_restart_keeps_its_agent_session(mcp_key, db_engine) -> None:  # noqa: F811
    from specivo.main import create_app
    from specivo.mcp.mount import get_mcp_session_manager

    raw_key, _user_id, key_id = mcp_key
    try:
        app = create_app()
        manager = get_mcp_session_manager()
        assert manager is not None
        async with manager.run():
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                headers = await _connect(client, raw_key)
                await _whoami(client, headers)
        before = await _session_ids(db_engine, key_id)
        assert len(before) == 1

        # A new process: the session is rebuilt from Redis under the same id.
        app = create_app()
        manager = get_mcp_session_manager()
        assert manager is not None
        async with manager.run():
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                await _whoami(client, headers)
        assert await _session_ids(db_engine, key_id) == before
    finally:
        await _cleanup(db_engine, key_id)
