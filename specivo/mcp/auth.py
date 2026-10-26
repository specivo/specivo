"""MCP auth bridge -- validates ``Bearer spv_...`` API keys for MCP HTTP transport.

Two-layer design for instant key revocation:

1. **Transport layer** (``SpvTokenVerifier``):
   Implements the MCP SDK's ``TokenVerifier`` protocol.  Called by
   Starlette auth middleware on every HTTP request (SSE GET/POST,
   Streamable HTTP POST).  Gates transport-level access and stashes
   the raw key in ``mcp_raw_key_var``.

2. **Tool layer** (``authenticate_mcp_tool``):
   Called at the start of every tool invocation.  Re-validates the
   raw key against the DB, catching revoked / expired / deactivated
   keys even within a long-lived SSE session.

The raw key travels through a contextvar, never logged or exposed.
"""

from __future__ import annotations

import contextvars
import hashlib
import logging

from mcp.server.auth.provider import AccessToken, TokenVerifier
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.constants import API_KEY_MIN_LENGTH, API_KEY_PREFIX, MCP_PENDING_CLIENT_ID
from specivo.core.exceptions import AppError
from specivo.models.auth import ApiKey
from specivo.models.user import User
from specivo.services.api_key_service import ApiKeyService

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Context variables
# ---------------------------------------------------------------------------

# Raw ``spv_...`` key for the current MCP request / session.
# Set by ``SpvTokenVerifier`` at transport level.
# Read by ``authenticate_mcp_tool`` at tool level.
mcp_raw_key_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("mcp_raw_key", default=None)

_api_key_svc = ApiKeyService()


def key_principal(raw_key: str) -> str:
    """Return a stable, non-reversible identifier for an API key.

    Used as the ``AccessToken.subject`` so the SDK binds every MCP session
    to the key that opened it: a request presenting a different key for a
    live session id is answered as if the session did not exist. The
    domain prefix keeps it distinct from the stored key hash.
    """
    return hashlib.sha256(b"specivo-mcp-session-owner:" + raw_key.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Transport-level auth  (MCP SDK TokenVerifier protocol)
# ---------------------------------------------------------------------------


class SpvTokenVerifier(TokenVerifier):
    """Format-only gate at the HTTP transport level.

    Only validates the ``spv_`` prefix and minimum length — no DB hit.
    The raw key is stashed in ``mcp_raw_key_var`` so every subsequent
    tool call can authenticate against the DB via ``authenticate_mcp_tool``.

    This avoids a double DB round-trip (transport + tool) on every
    MCP request.  The tool-level auth is the real security gate.
    """

    async def verify_token(self, token: str) -> AccessToken | None:
        # Quick format check — real auth happens per-tool call.
        if not token.startswith(API_KEY_PREFIX) or len(token) < API_KEY_MIN_LENGTH:
            return None

        # Stash the raw key -- tool-level auth will validate against DB.
        mcp_raw_key_var.set(token)

        return AccessToken(
            token=token,
            client_id=MCP_PENDING_CLIENT_ID,
            scopes=["mcp"],
            expires_at=None,
            subject=key_principal(token),
        )


async def resolve_key_owner(raw_key: str) -> tuple[int, int] | None:
    """Return ``(api_key_id, user_id)`` for a usable key, else ``None``.

    Runs the full DB check (revoked, expired, deactivated user), so a
    session bound to a key that is no longer valid cannot be resumed.
    Used when an MCP session is opened and when one is rehydrated after a
    restart; regular requests stay format-only at the transport level.
    """
    from specivo.core.database import get_session_factory

    async with get_session_factory()() as session:
        try:
            user, api_key = await _api_key_svc.authenticate(session, raw_key)
        except AppError:
            return None
        await session.commit()
        return api_key.id, user.id


# ---------------------------------------------------------------------------
# Tool-level auth  (called on every tool invocation)
# ---------------------------------------------------------------------------


async def authenticate_mcp_tool(session: AsyncSession) -> tuple[User, ApiKey]:
    """Re-authenticate the raw API key within *session*.

    This runs on **every** tool call, ensuring that a revoked or
    expired key is rejected immediately -- even mid-session on a
    long-lived SSE connection.

    Returns ``(user, api_key)`` or raises ``RuntimeError`` / ``AppError``.
    """
    raw_key = mcp_raw_key_var.get()
    if raw_key is None:
        raise RuntimeError("MCP request has no API key in context -- not authenticated")

    user, api_key = await _api_key_svc.authenticate(session, raw_key)
    return user, api_key
