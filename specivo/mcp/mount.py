"""Mount MCP server as HTTP endpoints inside the FastAPI application.

Provides two transports:

- **Streamable HTTP** at ``{prefix}/`` -- modern MCP transport (spec 2025-03-26).
  Stateless: each HTTP request is a complete MCP exchange.  Auth is
  re-validated on every request by Starlette middleware, and again inside
  each tool call by ``authenticate_mcp_tool``.

- **SSE** at ``{prefix}/sse`` -- legacy transport for older MCP clients.
  Long-lived GET connection + POST messages.  Auth middleware validates
  each HTTP request; tool-level auth re-checks the key on every tool call.

Both transports use ``Bearer spv_...`` API key authentication.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.middleware.bearer_auth import (
    BearerAuthBackend,
    RequireAuthMiddleware,
)
from mcp.server.fastmcp.server import StreamableHTTPASGIApp
from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Mount, Route, request_response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from specivo.mcp.auth import SpvTokenVerifier
from specivo.mcp.persistent_session_manager import (
    PersistentStreamableHTTPSessionManager,
)
from specivo.mcp.server import mcp

logger = logging.getLogger(__name__)


class _StripWWWAuthenticate:
    """ASGI wrapper that strips WWW-Authenticate: Bearer headers from 401 responses.

    Claude Code's MCP SDK interprets this header as a signal to discover
    OAuth metadata at /.well-known/oauth-authorization-server, /register,
    etc.  When the server uses simple Bearer token auth (not OAuth),
    returning this header causes the client to fail discovery instead
    of using its configured token.

    Stripping the header lets the client fall back to sending its
    preconfigured Authorization: Bearer header on the next request.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_filtered(message: Message) -> None:
            if message["type"] == "http.response.start" and message.get("status") == 401:
                headers = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if k.lower() != b"www-authenticate"
                ]
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_filtered)

_session_manager: StreamableHTTPSessionManager | None = None


async def _default_store_provider():  # type: ignore[no-untyped-def]
    """Lazy provider for the Redis-backed session store.

    Imports inside the body to avoid pulling redis at module-import time,
    and so that tests can swap the session store singleton without touching
    this module.
    """
    from specivo.mcp.session_store import get_mcp_session_store

    try:
        return await get_mcp_session_store()
    except Exception:
        logger.warning(
            "Unable to construct Redis-backed MCP session store; "
            "sessions will not survive api restarts",
            exc_info=True,
        )
        return None


def get_mcp_session_manager() -> StreamableHTTPSessionManager | None:
    """Return the session manager created by ``mount_mcp``.

    Used by the main app lifespan to start / stop the manager.
    """
    return _session_manager


def mount_mcp(app: FastAPI, *, prefix: str = "/mcp") -> None:
    """Mount MCP Streamable HTTP and SSE transports into *app*.

    Call this from ``create_app()`` **before** the web router (which has
    catch-all paths) but **after** the API router.

    The ``StreamableHTTPSessionManager`` requires a running lifespan
    context.  Since FastAPI does not propagate sub-app lifespans,
    the caller must wire ``get_mcp_session_manager().run()`` into the
    main app lifespan.
    """
    global _session_manager  # noqa: PLW0603

    token_verifier = SpvTokenVerifier()

    # ------------------------------------------------------------------
    # Streamable HTTP  (primary transport at {prefix}/)
    # ------------------------------------------------------------------

    # Stateful, with Redis-backed session continuity. See
    # ``PersistentStreamableHTTPSessionManager`` for the rehydration design
    # — this is how a Claude Code client that was talking to the server
    # before an api restart gets to keep using the same Mcp-Session-Id
    # without a client-side reconnect.
    _session_manager = PersistentStreamableHTTPSessionManager(
        app=mcp._mcp_server,
        event_store=None,
        json_response=False,
        stateless=False,
        store_provider=_default_store_provider,
    )
    mcp._session_manager = _session_manager

    streamable_asgi = StreamableHTTPASGIApp(_session_manager)

    # Wrap with RequireAuthMiddleware so unauthenticated requests get
    # a proper 401 before reaching the MCP handler.
    authed_streamable = _StripWWWAuthenticate(
        RequireAuthMiddleware(streamable_asgi, required_scopes=[])
    )

    streamable_app = Starlette(
        routes=[Route("/", endpoint=authed_streamable)],
        middleware=[
            Middleware(
                AuthenticationMiddleware,
                backend=BearerAuthBackend(token_verifier),
            ),
            Middleware(AuthContextMiddleware),
        ],
    )

    # ------------------------------------------------------------------
    # SSE  (legacy transport at {prefix}/sse)
    # ------------------------------------------------------------------

    # The endpoint path is the absolute URL the client will POST to.
    # SseServerTransport computes it from root_path + endpoint at runtime,
    # but we pass a path relative to the SSE sub-app mount.
    sse_transport = SseServerTransport("/messages/")

    async def handle_sse(request: Request) -> Response:
        async with sse_transport.connect_sse(
            request.scope,
            request.receive,
            request._send,
        ) as streams:
            await mcp._mcp_server.run(
                streams[0],
                streams[1],
                mcp._mcp_server.create_initialization_options(),
            )
        return Response()

    # Convert Starlette-style endpoint (request -> Response) to ASGI callable
    # before wrapping with ASGI middleware.
    handle_sse_asgi = request_response(handle_sse)

    # Require auth on both the SSE GET and the POST messages endpoint.
    authed_sse = _StripWWWAuthenticate(RequireAuthMiddleware(handle_sse_asgi, required_scopes=[]))
    authed_messages = _StripWWWAuthenticate(
        RequireAuthMiddleware(sse_transport.handle_post_message, required_scopes=[])
    )

    sse_app = Starlette(
        routes=[
            Route("/", endpoint=authed_sse, methods=["GET"]),
            Mount("/messages/", app=authed_messages),
        ],
        middleware=[
            Middleware(
                AuthenticationMiddleware,
                backend=BearerAuthBackend(token_verifier),
            ),
            Middleware(AuthContextMiddleware),
        ],
    )

    # ------------------------------------------------------------------
    # Mount into FastAPI  (more specific paths first)
    # ------------------------------------------------------------------

    app.mount(f"{prefix}/sse", sse_app)
    app.mount(prefix, streamable_app)

    logger.info(
        "MCP mounted: Streamable HTTP at %s/, SSE at %s/sse",
        prefix,
        prefix,
    )
