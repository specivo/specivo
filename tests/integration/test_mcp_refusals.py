"""A refused MCP tool call is a tool error, and nothing it wrote is committed.

Tools refuse by raising (``ValidationError``, ``NotFoundError``, a
``ValueError`` from an argument check, ...). ``_get_session_and_user`` rolls
the call's transaction back and turns the exception into a ``ToolError``, so
the client sees ``isError=true`` with the message, and any row written before
the refusal is gone.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError
from sqlalchemy import delete, func, select

from specivo.core import database
from specivo.core.exceptions import ValidationError
from specivo.mcp import server as mcp_server_module
from specivo.mcp.auth import mcp_raw_key_var
from specivo.mcp.server import _get_session_and_user, mcp
from specivo.mcp.tools import _log_tool
from specivo.models.agent_session import AgentSession
from specivo.models.security_audit import SecurityAuditLog
from tests.integration.test_mcp_transport import mcp_key  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.serial, pytest.mark.asyncio(loop_scope="function")]


@pytest.fixture(autouse=True)
def _tool_engine(db_engine) -> Iterator[None]:
    """Run the tool calls' own sessions on this test's engine and event loop."""
    saved = database._engine
    database.set_engine(db_engine)
    yield
    database.set_engine(saved)  # type: ignore[arg-type]


async def _audit_rows(db_engine, tool: str) -> int:
    async with db_engine.connect() as conn:
        return int(
            await conn.scalar(
                select(func.count())
                .select_from(SecurityAuditLog)
                .where(SecurityAuditLog.details["tool"].astext == tool)
            )
            or 0
        )


async def _agent_sessions(db_engine, key_id: int) -> int:
    async with db_engine.connect() as conn:
        return int(
            await conn.scalar(select(func.count()).select_from(AgentSession).where(AgentSession.api_key_id == key_id))
            or 0
        )


async def test_a_write_before_a_refusal_is_rolled_back(mcp_key, db_engine) -> None:  # noqa: F811
    raw_key, _user_id, _key_id = mcp_key
    token = mcp_raw_key_var.set(raw_key)
    try:
        with pytest.raises(ToolError, match="^the refusal message$"):
            async with _get_session_and_user() as (session, user):
                await _log_tool(session, user, "resource_access", "refusal_probe")
                raise ValidationError("the refusal message")
        assert await _audit_rows(db_engine, "refusal_probe") == 0

        # The same write without a refusal is committed.
        async with _get_session_and_user() as (session, user):
            await _log_tool(session, user, "resource_access", "refusal_probe")
        assert await _audit_rows(db_engine, "refusal_probe") == 1
    finally:
        mcp_raw_key_var.reset(token)
        async with db_engine.begin() as conn:
            await conn.execute(
                delete(SecurityAuditLog).where(SecurityAuditLog.details["tool"].astext == "refusal_probe")
            )


async def test_a_refusal_reaches_the_client_as_a_tool_error(
    mcp_key,  # noqa: F811
    db_engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Over the protocol a refusal is ``isError``, with the message, and the call leaves nothing behind."""
    raw_key, _user_id, key_id = mcp_key
    real_authenticate = mcp_server_module.authenticate_mcp_tool

    async def authenticate(session: Any) -> Any:
        # The in-memory client has no HTTP layer to stash the key.
        token = mcp_raw_key_var.set(raw_key)
        try:
            return await real_authenticate(session)
        finally:
            mcp_raw_key_var.reset(token)

    monkeypatch.setattr(mcp_server_module, "authenticate_mcp_tool", authenticate)

    async with Client(mcp) as client:
        refused = await client.call_tool(
            "specivo_metadata", {"target_ref": "NOPE-1", "key": "commits", "op": "frobnicate"}
        )
        assert refused.is_error is True
        assert refused.content[0].text == (  # type: ignore[union-attr]
            "Error executing tool specivo_metadata: invalid op 'frobnicate'. "
            "Must be one of: append, delete, get, remove, set"
        )
        # The agent session the call opened was written before the refusal: rolled back with it.
        assert await _agent_sessions(db_engine, key_id) == 0

        answered = await client.call_tool("specivo_whoami", {})
        assert answered.is_error is False
        assert await _agent_sessions(db_engine, key_id) == 1

    async with db_engine.begin() as conn:
        await conn.execute(delete(AgentSession).where(AgentSession.api_key_id == key_id))
        await conn.execute(
            delete(SecurityAuditLog).where(SecurityAuditLog.details["tool"].astext.in_(["whoami", "metadata"]))
        )
