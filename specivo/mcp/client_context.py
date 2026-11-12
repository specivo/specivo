"""Per-call MCP context: the calling client, and the agent session a tool call belongs to.

``ClientInfoMiddleware`` runs around every inbound MCP request. It reads the
client's ``clientInfo`` from the ``initialize`` handshake the connection
holds, else the first product of the HTTP ``User-Agent`` (a session
rehydrated after a restart, or a client that sent no ``clientInfo``), and
publishes it in ``mcp_client_var`` for the duration of the request. It also
publishes the connection the request came on, its transport session id, in
``mcp_connection_var``: the ``Mcp-Session-Id`` header for Streamable HTTP
(the same id after the session is restored from Redis), the ``session_id``
query parameter of a legacy SSE message post.

``_get_session_and_user`` in ``specivo.mcp.server`` passes that client and
connection to ``AgentSessionService.get_or_create_session`` (one agent session
per connection) and publishes the resulting
agent session id in ``agent_session_id_var`` for the duration of the tool
call, so tools can record the issue the session is working on
(``record_working_issue``) without threading it through every signature.
"""

from __future__ import annotations

import contextvars
import logging
from typing import TYPE_CHECKING, Any

from specivo.services.agent_session_service import AgentSessionService, ClientInfo, parse_user_agent

if TYPE_CHECKING:
    from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
    from sqlalchemy.ext.asyncio import AsyncSession

    from specivo.models.issue import Issue
    from specivo.models.user import User

logger = logging.getLogger(__name__)

mcp_client_var: contextvars.ContextVar[ClientInfo | None] = contextvars.ContextVar("mcp_client", default=None)
#: The transport session id of the connection the current MCP request came on.
mcp_connection_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("mcp_connection", default=None)
agent_session_id_var: contextvars.ContextVar[int | None] = contextvars.ContextVar("mcp_agent_session_id", default=None)
#: The id of the API key that authenticated the current tool call (set by the tool session helper).
mcp_api_key_id_var: contextvars.ContextVar[int | None] = contextvars.ContextVar("mcp_api_key_id", default=None)

_agent_session_svc = AgentSessionService()


def client_from_request_context(ctx: ServerRequestContext[Any, Any]) -> ClientInfo | None:
    """Return the client of an MCP request: its ``clientInfo``, else its HTTP User-Agent."""
    try:
        params = ctx.session.client_params
    except Exception:  # pragma: no cover - defensive: never fail a request over telemetry  # noqa: BLE001
        params = None
    if params is not None:
        info = ClientInfo.of(params.client_info.name, params.client_info.version)
        if info is not None:
            return info
    headers = getattr(ctx.request, "headers", None)
    if headers is None:
        return None
    return parse_user_agent(headers.get("user-agent"))


_MCP_SESSION_ID_HEADER = "mcp-session-id"
_SSE_SESSION_ID_PARAM = "session_id"


def connection_from_request_context(ctx: ServerRequestContext[Any, Any]) -> str | None:
    """Return the transport session id of the connection an MCP request came on.

    ``Mcp-Session-Id`` for Streamable HTTP, the ``session_id`` query parameter
    for a legacy SSE message post; ``None`` without either (an in-process
    client, or a request with no HTTP layer).
    """
    request = getattr(ctx, "request", None)
    if request is None:
        return None
    headers = getattr(request, "headers", None)
    value = headers.get(_MCP_SESSION_ID_HEADER) if headers is not None else None
    if not value:
        query = getattr(request, "query_params", None)
        value = query.get(_SSE_SESSION_ID_PARAM) if query is not None else None
    value = (value or "").strip()
    return value or None


class ClientInfoMiddleware:
    """MCP server middleware publishing the calling client and its connection.

    The client goes to ``mcp_client_var``, the connection's transport session
    id to ``mcp_connection_var``, both for the duration of the request.
    """

    async def __call__(self, ctx: ServerRequestContext[Any, Any], call_next: CallNext) -> HandlerResult:
        if ctx.request_id is None:
            # A notification: no tool runs inside it.
            return await call_next(ctx)
        try:
            client = client_from_request_context(ctx)
        except Exception:
            logger.debug("Reading the MCP client failed", exc_info=True)
            client = None
        try:
            connection = connection_from_request_context(ctx)
        except Exception:
            logger.debug("Reading the MCP connection failed", exc_info=True)
            connection = None
        token = mcp_client_var.set(client)
        connection_token = mcp_connection_var.set(connection)
        try:
            return await call_next(ctx)
        finally:
            mcp_connection_var.reset(connection_token)
            mcp_client_var.reset(token)


async def record_working_issue(session: AsyncSession, user: User, issue: Issue) -> bool:
    """Record *issue* as the one the current tool call's agent session is working on.

    A no-op returning ``False`` outside a tracked tool call. Failures are
    logged and swallowed: attribution never fails the tool.
    """
    agent_session_id = agent_session_id_var.get()
    if agent_session_id is None:
        return False
    # A savepoint, so a failure here cannot poison the tool's transaction.
    savepoint = await session.begin_nested()
    try:
        recorded = await _agent_session_svc.record_issue(session, agent_session_id, user, issue)
        await savepoint.commit()
        return recorded
    except Exception:
        if savepoint.is_active:
            await savepoint.rollback()
        logger.warning("Recording the agent session's issue failed", exc_info=True)
        return False
