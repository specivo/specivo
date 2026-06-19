"""Streamable HTTP session manager with Redis-backed session continuity.

Problem
-------
The upstream ``mcp.server.streamable_http_manager.StreamableHTTPSessionManager``
tracks live sessions in an in-memory ``dict[str, StreamableHTTPServerTransport]``
called ``_server_instances``. When the api process restarts, that dict is
empty. Any client that comes back with a previously issued ``Mcp-Session-Id``
falls into the SDK's 404 branch ("Session not found") and has to reinitialize.
For a long-running MCP client (e.g. Claude Code) attached to a container that
gets redeployed multiple times a day, this shows up as a stream of broken
connections the user has to hand-repair.

Solution
--------
This subclass hooks the ``_handle_stateful_request`` dispatch in three places:

1. **On new-session creation** — after the SDK assigns a fresh session id,
   persist ``{session_id, api_key_id, user_id, ...}`` to Redis via
   :class:`~specivo.mcp.session_store.RedisSessionStore`.

2. **On a request that carries a known session id** — schedule a background
   ``store.touch(session_id)`` so the sliding TTL extends without blocking the
   response. If Redis is down the touch is logged and swallowed.

3. **On a request that carries a session id the in-memory dict does not know
   about** — before giving up with 404, ask Redis. If Redis has the session,
   mint a fresh ``StreamableHTTPServerTransport`` under the *same* session id,
   register it in ``_server_instances``, start its server task, refresh the
   TTL, and let the request proceed. From the client's point of view, the
   session survived the restart.

What is persistent vs ephemeral
-------------------------------
Only the **identity** of a session is persistent across restarts: the
``Mcp-Session-Id`` string and the metadata tied to it. The live anyio streams,
the running task that drives ``Server.run``, and any in-flight JSON-RPC state
on a per-connection basis are inherently per-process and **cannot** be
serialized. That is a property of the SDK transport, not a choice we make.

The practical consequence is: the *first* request after a restart that arrives
on a previously known session id pays the cost of standing up a new transport
(microseconds), and any *in-flight* request that was mid-flight at the moment
the process died is lost — the client will have to retry it. Every subsequent
request on that session behaves identically to one on a session that was
never interrupted.

Failure modes
-------------
* **Redis unavailable at rehydration time** — we log at WARNING and fall
  through to the SDK's original 404 path. The client gets the same behaviour
  it would have seen without this shim; we do not crash the request.
* **Redis unavailable at session creation** — we log at WARNING and let the
  session proceed without a persisted entry. The session works normally in
  the current process but will not survive a restart. This is strictly better
  than failing the initialize outright.
* **Concurrent rehydration of the same session id** — protected by the same
  ``_session_creation_lock`` the SDK already uses for new sessions.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from http import HTTPStatus
from typing import TYPE_CHECKING

import anyio
from anyio.abc import TaskStatus
from mcp.server.streamable_http import (
    MCP_SESSION_ID_HEADER,
    StreamableHTTPServerTransport,
)
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import INVALID_REQUEST, ErrorData, JSONRPCError
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import Receive, Scope, Send

if TYPE_CHECKING:
    from specivo.mcp.session_store import RedisSessionStore

logger = logging.getLogger(__name__)


StoreProvider = Callable[[], Awaitable["RedisSessionStore | None"]]
"""Async callable that returns the active session store, or ``None`` if unavailable.

We take a provider rather than a store instance because the store depends on
the shared Redis client, which is created during app startup — after the
session manager itself is constructed in ``mount_mcp``.
"""


class PersistentStreamableHTTPSessionManager(StreamableHTTPSessionManager):
    """Stateful session manager that persists session ids across restarts."""

    def __init__(
        self,
        *args,  # type: ignore[no-untyped-def]
        store_provider: StoreProvider | None = None,
        **kwargs,  # type: ignore[no-untyped-def]
    ) -> None:
        super().__init__(*args, **kwargs)
        self._store_provider: StoreProvider | None = store_provider
        if self.stateless:
            # The whole point of this subclass is stateful session continuity.
            # Running in stateless mode would silently disable it.
            raise ValueError("PersistentStreamableHTTPSessionManager requires stateless=False")

    # ------------------------------------------------------------------
    # Public hook: swap in the store provider after construction
    # ------------------------------------------------------------------

    def set_store_provider(self, provider: StoreProvider) -> None:
        """Wire in the async store provider. Called from app lifespan."""
        self._store_provider = provider

    # ------------------------------------------------------------------
    # Internal helpers around the store, all of which fail open
    # ------------------------------------------------------------------

    async def _get_store(self) -> RedisSessionStore | None:
        if self._store_provider is None:
            return None
        try:
            return await self._store_provider()
        except Exception:  # pragma: no cover - defensive
            logger.warning("MCP session store provider raised", exc_info=True)
            return None

    async def _persist_new_session(self, session_id: str, scope: Scope) -> None:
        store = await self._get_store()
        if store is None:
            return
        try:
            await store.create(
                session_id,
                self._metadata_from_scope(scope),
            )
        except Exception:
            logger.warning(
                "Failed to persist MCP session %s to Redis; continuing without",
                session_id,
                exc_info=True,
            )

    async def _touch_session(self, session_id: str) -> None:
        store = await self._get_store()
        if store is None:
            return
        try:
            await store.touch(session_id)
        except Exception:
            logger.debug("Failed to touch MCP session %s in Redis", session_id, exc_info=True)

    async def _lookup_session(self, session_id: str) -> dict[str, str] | None:
        store = await self._get_store()
        if store is None:
            return None
        try:
            return await store.get(session_id)
        except Exception:
            logger.warning(
                "MCP session lookup for %s failed; falling through to 404",
                session_id,
                exc_info=True,
            )
            return None

    @staticmethod
    def _metadata_from_scope(scope: Scope) -> dict[str, object]:
        """Extract the bits we can safely pull from the ASGI scope at create time.

        The authenticated user context is already attached to the scope by
        ``AuthenticationMiddleware`` / ``AuthContextMiddleware`` upstream of
        this handler. We grab what we can; anything missing is simply not
        persisted, and the entry can be enriched later via ``touch``.
        """
        meta: dict[str, object] = {"transport": "streamable_http"}
        user = scope.get("user")
        if user is not None:
            for attr in ("api_key_id", "user_id"):
                value = getattr(user, attr, None)
                if value is not None:
                    meta[attr] = value
        headers = dict(scope.get("headers") or [])
        ua = headers.get(b"user-agent")
        if ua:
            try:
                meta["user_agent"] = ua.decode("latin-1")
            except Exception:  # pragma: no cover
                pass
        return meta

    # ------------------------------------------------------------------
    # Overridden dispatch — the critical path
    # ------------------------------------------------------------------

    async def _handle_stateful_request(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        """Handle a stateful request with Redis-backed session continuity.

        Structurally this mirrors the upstream implementation in
        ``StreamableHTTPSessionManager._handle_stateful_request`` but adds
        three hooks:

        * persist new session ids to Redis
        * touch known session ids on every hit
        * rehydrate a fresh transport when the in-memory dict does not know
          a session id but Redis does
        """
        request = Request(scope, receive)
        request_mcp_session_id = request.headers.get(MCP_SESSION_ID_HEADER)

        # ---- Existing in-memory session ---------------------------------
        if request_mcp_session_id is not None and request_mcp_session_id in self._server_instances:
            transport = self._server_instances[request_mcp_session_id]
            logger.debug("Session already exists, handling request directly")
            # Refresh sliding TTL in the background; do not block the hot path.
            if self._task_group is not None:
                self._task_group.start_soon(self._touch_session, request_mcp_session_id)
            await transport.handle_request(scope, receive, send)
            return

        # ---- Brand-new session (no header at all) -----------------------
        if request_mcp_session_id is None:
            await self._create_and_run_session(scope, receive, send, session_id=None)
            return

        # ---- Header present but unknown to in-memory dict ---------------
        # Ask Redis before giving up. This is the restart-survival path.
        persisted = await self._lookup_session(request_mcp_session_id)
        if persisted is not None:
            logger.info(
                "Rehydrating MCP session %s from Redis after in-memory miss",
                request_mcp_session_id,
            )
            await self._create_and_run_session(
                scope,
                receive,
                send,
                session_id=request_mcp_session_id,
            )
            return

        # ---- Genuinely unknown — mirror upstream 404 --------------------
        error_response = JSONRPCError(
            jsonrpc="2.0",
            id="server-error",
            error=ErrorData(
                code=INVALID_REQUEST,
                message="Session not found",
            ),
        )
        response = Response(
            content=error_response.model_dump_json(by_alias=True, exclude_none=True),
            status_code=HTTPStatus.NOT_FOUND,
            media_type="application/json",
        )
        await response(scope, receive, send)

    # ------------------------------------------------------------------
    # Shared create-or-rehydrate path
    # ------------------------------------------------------------------

    async def _create_and_run_session(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        session_id: str | None,
    ) -> None:
        """Create a fresh transport (either brand new or rehydrated).

        If ``session_id`` is ``None`` a new UUID4 id is minted (brand-new
        session) and persisted to Redis. If ``session_id`` is provided, the
        transport is created under that id (rehydration) and the Redis entry
        is touched to refresh its TTL — the entry already exists because the
        caller looked it up.
        """
        from uuid import uuid4

        is_rehydration = session_id is not None
        assert self._task_group is not None

        async with self._session_creation_lock:
            # Race: another coroutine may have just rehydrated/created the
            # same session id while we were waiting on the lock.
            if session_id is not None and session_id in self._server_instances:
                transport = self._server_instances[session_id]
                await transport.handle_request(scope, receive, send)
                return

            new_session_id = session_id or uuid4().hex
            http_transport = StreamableHTTPServerTransport(
                mcp_session_id=new_session_id,
                is_json_response_enabled=self.json_response,
                event_store=self.event_store,
                security_settings=self.security_settings,
                retry_interval=self.retry_interval,
            )
            assert http_transport.mcp_session_id is not None
            self._server_instances[new_session_id] = http_transport

            if is_rehydration:
                logger.info(
                    "Created rehydrated transport under existing session id %s",
                    new_session_id,
                )
            else:
                logger.info("Created new transport with session ID: %s", new_session_id)

            async def run_server(*, task_status: TaskStatus[None] = anyio.TASK_STATUS_IGNORED) -> None:
                async with http_transport.connect() as streams:
                    read_stream, write_stream = streams
                    task_status.started()
                    try:
                        # Rehydrated sessions must come up already-initialized.
                        #
                        # The SDK keeps the ``initialize`` handshake result in
                        # the per-process ``ServerSession._initialization_state``;
                        # it is never persisted and cannot be serialized. A
                        # brand-new ``ServerSession`` starts ``NotInitialized``
                        # and rejects every non-``initialize`` request with
                        # ``Received request before initialization was complete``
                        # (surfaced to the client as JSON-RPC ``-32602``).
                        #
                        # A client whose session predates an api restart already
                        # finished its handshake and will only send regular
                        # requests, never a second ``initialize`` — so a fresh
                        # ``NotInitialized`` session would be permanently stuck.
                        #
                        # Running with ``stateless=True`` makes the SDK seed the
                        # session as ``Initialized`` (see
                        # ``ServerSession.__init__``). The transport itself stays
                        # fully stateful (same ``Mcp-Session-Id``, SSE/JSON, event
                        # store); ``stateless`` here only governs the init-state
                        # gate. This is safe because Specivo's MCP tools carry no
                        # per-session negotiated state — auth is re-validated from
                        # the Bearer key on every tool call.
                        await self.app.run(
                            read_stream,
                            write_stream,
                            self.app.create_initialization_options(),
                            stateless=is_rehydration,
                        )
                    except Exception as e:
                        logger.error(
                            "Session %s crashed: %s",
                            http_transport.mcp_session_id,
                            e,
                            exc_info=True,
                        )
                    finally:
                        if (
                            http_transport.mcp_session_id
                            and http_transport.mcp_session_id in self._server_instances
                            and not http_transport.is_terminated
                        ):
                            logger.info(
                                "Cleaning up crashed session %s from active instances",
                                http_transport.mcp_session_id,
                            )
                            del self._server_instances[http_transport.mcp_session_id]

            await self._task_group.start(run_server)

            # Persist new sessions; for rehydration the entry already exists,
            # so just refresh the sliding TTL.
            if is_rehydration:
                await self._touch_session(new_session_id)
            else:
                await self._persist_new_session(new_session_id, scope)

            await http_transport.handle_request(scope, receive, send)
