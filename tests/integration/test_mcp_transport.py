"""Integration tests for MCP HTTP transport auth.

Tests cover:
- Streamable HTTP auth (no key, invalid key, non-spv prefix → 401)
- SSE auth (no key, invalid key → 401)
- Valid key + auth contextvar propagation (unit test of SpvTokenVerifier)
- Key revocation invalidates access

NOTE: MCP auth middleware opens its own DB connection (not the test
transaction), so API keys must be committed via a separate connection.
Tests that need a valid key use a committed fixture with cleanup.
Full tool execution tests are in test_mcp_server.py (direct tool calls).
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.auth import ApiKey
from specivo.models.user import User
from specivo.services.api_key_service import ApiKeyService

pytestmark = [pytest.mark.integration, pytest.mark.serial]

MCP_URL = "/mcp/"
MCP_SSE_URL = "/mcp/sse/"

_INIT_PARAMS = {
    "protocolVersion": "2025-03-26",
    "capabilities": {},
    "clientInfo": {"name": "test", "version": "1.0"},
}


def _mcp_request(method: str, params: dict | None = None, req_id: int = 1) -> dict:
    msg: dict = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


@pytest_asyncio.fixture
async def mcp_key(db_engine) -> tuple[str, int, int]:
    """Create a committed API key visible to MCP's own DB session."""
    async with db_engine.begin() as conn:
        session = AsyncSession(bind=conn, expire_on_commit=False)
        user = User(
            login="mcp_test_user",
            email="mcp_test@example.com",
            display_name="MCP Test",
            status="active",
            is_service_account=True,
        )
        session.add(user)
        await session.flush()

        svc = ApiKeyService()
        key_record, raw_key = await svc.create_key(session, user.id, name="mcp-test")
        await session.flush()
        user_id = user.id
        key_id = key_record.id
        await session.commit()

    yield raw_key, user_id, key_id

    async with db_engine.begin() as conn:
        await conn.execute(delete(ApiKey).where(ApiKey.id == key_id))
        await conn.execute(delete(User).where(User.id == user_id))
        await conn.commit()


# ---------------------------------------------------------------------------
# Streamable HTTP — Auth rejection (no session manager needed for 401s)
# ---------------------------------------------------------------------------


class TestStreamableHTTPAuthRejection:
    async def test_no_auth_returns_401(self, client: AsyncClient):
        resp = await client.post(MCP_URL, json=_mcp_request("initialize", _INIT_PARAMS))
        assert resp.status_code == 401

    async def test_invalid_key_returns_401(self, client: AsyncClient):
        resp = await client.post(
            MCP_URL,
            json=_mcp_request("initialize", _INIT_PARAMS),
            headers={"Authorization": "Bearer spv_invalid_key_xyz"},
        )
        assert resp.status_code == 401

    async def test_non_spv_prefix_returns_401(self, client: AsyncClient):
        resp = await client.post(
            MCP_URL,
            json=_mcp_request("initialize", _INIT_PARAMS),
            headers={"Authorization": "Bearer not_a_spv_key"},
        )
        assert resp.status_code == 401

    async def test_missing_bearer_returns_401(self, client: AsyncClient):
        resp = await client.post(
            MCP_URL,
            json=_mcp_request("initialize", _INIT_PARAMS),
            headers={"Authorization": "spv_no_bearer_prefix"},
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# SSE — Auth rejection
# ---------------------------------------------------------------------------


class TestSSEAuthRejection:
    async def test_sse_no_auth_returns_401(self, client: AsyncClient):
        resp = await client.get(MCP_SSE_URL)
        assert resp.status_code == 401

    async def test_sse_invalid_key_returns_401(self, client: AsyncClient):
        resp = await client.get(
            MCP_SSE_URL,
            headers={"Authorization": "Bearer spv_invalid_key_xyz"},
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# SpvTokenVerifier — unit test with real DB
# ---------------------------------------------------------------------------


class TestSpvTokenVerifier:
    async def test_valid_key_returns_access_token(self, mcp_key):
        """SpvTokenVerifier.verify_token returns AccessToken for valid format key.

        Transport-level auth is format-only (no DB hit).  Real auth
        happens at tool-level via authenticate_mcp_tool.
        """
        from specivo.core.constants import MCP_PENDING_CLIENT_ID
        from specivo.mcp.auth import SpvTokenVerifier

        raw_key, _user_id, _ = mcp_key
        verifier = SpvTokenVerifier()
        result = await verifier.verify_token(raw_key)
        assert result is not None
        assert result.client_id == MCP_PENDING_CLIENT_ID

    async def test_valid_format_but_unknown_key_passes_transport(self):
        """Transport auth accepts any key with valid spv_ format.

        Unknown keys are rejected at tool-level, not transport-level.
        """
        from specivo.mcp.auth import SpvTokenVerifier

        verifier = SpvTokenVerifier()
        result = await verifier.verify_token("spv_does_not_exist_but_long_enough_key_1234567890")
        assert result is not None  # transport accepts — tool-level rejects

    async def test_non_spv_key_returns_none(self):
        from specivo.mcp.auth import SpvTokenVerifier

        verifier = SpvTokenVerifier()
        result = await verifier.verify_token("not_a_spv_key")
        assert result is None

    async def test_too_short_key_returns_none(self):
        from specivo.mcp.auth import SpvTokenVerifier

        verifier = SpvTokenVerifier()
        result = await verifier.verify_token("spv_short")
        assert result is None

    async def test_verify_sets_contextvar(self, mcp_key):
        from specivo.mcp.auth import SpvTokenVerifier, mcp_raw_key_var

        raw_key, _, _ = mcp_key
        verifier = SpvTokenVerifier()
        await verifier.verify_token(raw_key)
        assert mcp_raw_key_var.get() == raw_key


# ---------------------------------------------------------------------------
# authenticate_mcp_tool — re-validates key per tool call
# ---------------------------------------------------------------------------


class TestAuthenticateMcpTool:
    async def test_valid_key_returns_user(self, mcp_key, db_engine):
        from specivo.mcp.auth import authenticate_mcp_tool, mcp_raw_key_var

        raw_key, user_id, _ = mcp_key
        mcp_raw_key_var.set(raw_key)

        async with db_engine.begin() as conn:
            session = AsyncSession(bind=conn, expire_on_commit=False)
            user, api_key = await authenticate_mcp_tool(session)
            assert user.id == user_id

    async def test_revoked_key_raises(self, mcp_key, db_engine):
        from specivo.mcp.auth import authenticate_mcp_tool, mcp_raw_key_var

        raw_key, _, key_id = mcp_key
        mcp_raw_key_var.set(raw_key)

        # Deactivate the key
        async with db_engine.begin() as conn:
            await conn.execute(
                text("UPDATE api_keys SET is_active = false WHERE id = :kid"),
                {"kid": key_id},
            )
            await conn.commit()

        # Should raise
        async with db_engine.begin() as conn:
            session = AsyncSession(bind=conn, expire_on_commit=False)
            with pytest.raises(Exception):
                await authenticate_mcp_tool(session)

    async def test_no_contextvar_raises(self, db_engine):
        from specivo.mcp.auth import authenticate_mcp_tool, mcp_raw_key_var

        mcp_raw_key_var.set(None)

        async with db_engine.begin() as conn:
            session = AsyncSession(bind=conn, expire_on_commit=False)
            with pytest.raises(RuntimeError, match="no API key in context"):
                await authenticate_mcp_tool(session)
