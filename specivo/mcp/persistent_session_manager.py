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
   register it in ``_server_instances``, start its server task **seeded as
   already-initialized** (its ``Connection`` has the ``initialized`` event
   set), refresh the TTL, and let the request proceed. A client that completed its
   ``initialize`` handshake before the restart keeps working without a
   reconnect — from its point of view, the session survived the restart.

Ownership
---------
A session id is not a credential. Every session is bound to the API key
that opened it: live sessions through the SDK's owner check (each key has
its own ``AccessToken.subject``), persisted ones through the
``api_key_id`` / ``user_id`` stored with the entry. A rehydration re-checks
the request's key against the database and must resolve to that owner, so
a different key, a revoked or expired key, a deactivated user, a request
without a key, or an entry with no recorded owner all get the same 404 as
an unknown id.

What is persistent vs ephemeral
-------------------------------
Only the **identity** of a session is persistent across restarts: the
``Mcp-Session-Id`` string and the metadata tied to it. The live anyio streams,
the running task that drives ``Server.run``, and any in-flight JSON-RPC state
on a per-connection basis are inherently per-process and **cannot** be
serialized. That is a property of the SDK transport, not a choice we make.

The one piece of per-process handshake state we *do* recover is the
``initialize`` gate: a rehydrated session's ``Connection`` is created with
its ``initialized`` event already set, so the SDK's request gate is open.
Without this, a client that already finished its handshake before the
restart — and therefore only sends regular requests, never a second
``initialize`` — would be permanently rejected with ``-32602 / "Invalid
request parameters"``.

The same path also brings back sessions the SDK reaped in-process after
``session_idle_timeout``: the Redis entry outlives the in-memory transport,
so an idle client's next request is rehydrated instead of answered with 404.

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
from typing import TYPE_CHECKING, Any

import anyio
from anyio.abc import TaskStatus
from mcp.server.auth.middleware.bearer_auth import (
    AuthenticatedUser,
    AuthorizationContext,
    authorization_context,
)
from mcp.server.connection import Connection
from mcp.server.runner import serve_connection, serve_loop
from mcp.server.streamable_http import (
    MCP_SESSION_ID_HEADER,
    StreamableHTTPServerTransport,
)
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.shared.inbound import MCP_PROTOCOL_VERSION_HEADER
from mcp.shared.jsonrpc_dispatcher import JSONRPCDispatcher
from mcp.shared.message import SessionMessage
from mcp.shared.transport_context import TransportContext
from mcp.types import INVALID_REQUEST, ErrorData, JSONRPCError
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import Receive, Scope, Send

if TYPE_CHECKING:
    from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
    from mcp.server.lowlevel.server import Server

    from specivo.mcp.session_store import RedisSessionStore

logger = logging.getLogger(__name__)


StoreProvider = Callable[[], Awaitable["RedisSessionStore | None"]]
"""Async callable that returns the active session store, or ``None`` if unavailable.

We take a provider rather than a store instance because the store depends on
the shared Redis client, which is created during app startup — after the
session manager itself is constructed in ``mount_mcp``.
"""

OwnerResolver = Callable[[str], Awaitable["tuple[int, int] | None"]]
"""Async callable mapping a raw API key to ``(api_key_id, user_id)``.

Returns ``None`` when the key is unknown, revoked, expired or its user is
deactivated. Sessions are persisted with this owner, and a session is only
rehydrated for a request whose key resolves to the same owner.
"""


def _short(session_id: str) -> str:
    """Session id prefix for log lines; the full id is a bearer secret."""
    return f"{session_id[:8]}..."


def _bearer_token(scope: Scope) -> str | None:
    """The raw API key the auth middleware validated for this request."""
    user = scope.get("user")
    if isinstance(user, AuthenticatedUser):
        return user.access_token.token
    return None


async def _serve_session(
    server: Server[Any],
    read_stream: MemoryObjectReceiveStream[SessionMessage | Exception],
    write_stream: MemoryObjectSendStream[SessionMessage],
    *,
    lifespan_state: Any,
    session_id: str,
    rehydrated: bool,
    protocol_version: str | None,
) -> None:
    """Drive one stateful session's server loop over its transport streams.

    A brand-new session goes through the SDK's ``serve_loop`` unchanged. A
    rehydrated one gets the same dispatcher recipe, but its ``Connection``
    is born initialized: the client completed ``initialize`` before the
    restart and will not repeat it. Specivo's tools carry no per-session
    negotiated state (auth is re-validated from the Bearer key on every
    tool call), so skipping the handshake loses nothing.
    """
    if not rehydrated:
        await serve_loop(
            server,
            read_stream,
            write_stream,
            lifespan_state=lifespan_state,
            session_id=session_id,
        )
        return

    dispatcher: JSONRPCDispatcher[TransportContext] = JSONRPCDispatcher(
        read_stream,
        write_stream,
        inline_methods=frozenset({"initialize"}),
    )
    connection = Connection.for_loop(
        dispatcher,
        session_id=session_id,
        protocol_version_hint=protocol_version,
    )
    connection.initialized.set()
    await serve_connection(server, dispatcher, connection=connection, lifespan_state=lifespan_state)


def _session_not_found() -> Response:
    """The 404 the SDK answers for an unknown session id (JSON-RPC error, no id)."""
    body = JSONRPCError(
        jsonrpc="2.0",
        id=None,
        error=ErrorData(code=INVALID_REQUEST, message="Session not found"),
    )
    return Response(
        content=body.model_dump_json(by_alias=True, exclude_unset=True),
        status_code=HTTPStatus.NOT_FOUND,
        media_type="application/json",
    )


class PersistentStreamableHTTPSessionManager(StreamableHTTPSessionManager):
    """Stateful session manager that persists session ids across restarts."""

    def __init__(
        self,
        *args,  # type: ignore[no-untyped-def]
        store_provider: StoreProvider | None = None,
        owner_resolver: OwnerResolver | None = None,
        **kwargs,  # type: ignore[no-untyped-def]
    ) -> None:
        super().__init__(*args, **kwargs)
        self._store_provider: StoreProvider | None = store_provider
        # Without a resolver no session can prove its owner, so nothing is
        # persisted and nothing is rehydrated (fail closed).
        self._owner_resolver: OwnerResolver | None = owner_resolver
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

    async def _resolve_owner(self, scope: Scope) -> tuple[int, int] | None:
        """Resolve the request's API key to ``(api_key_id, user_id)``, or ``None``."""
        token = _bearer_token(scope)
        if token is None or self._owner_resolver is None:
            return None
        try:
            return await self._owner_resolver(token)
        except Exception:
            logger.warning("MCP session owner lookup failed", exc_info=True)
            return None

    async def _persist_new_session(self, session_id: str, scope: Scope) -> None:
        store = await self._get_store()
        if store is None:
            return
        owner = await self._resolve_owner(scope)
        if owner is None:
            # An entry without a verified owner could never be rehydrated,
            # so do not write one.
            logger.debug("MCP session %s has no verified owner; not persisted", _short(session_id))
            return
        api_key_id, user_id = owner
        metadata = self._metadata_from_scope(scope)
        metadata["api_key_id"] = api_key_id
        metadata["user_id"] = user_id
        try:
            await store.create(session_id, metadata)
        except Exception:
            logger.warning(
                "Failed to persist MCP session %s to Redis; continuing without",
                _short(session_id),
                exc_info=True,
            )

    async def _touch_session(self, session_id: str) -> None:
        store = await self._get_store()
        if store is None:
            return
        try:
            await store.touch(session_id)
        except Exception:
            logger.debug("Failed to touch MCP session %s in Redis", _short(session_id), exc_info=True)

    async def _lookup_session(self, session_id: str) -> dict[str, str] | None:
        store = await self._get_store()
        if store is None:
            return None
        try:
            # ``peek`` rather than ``get``: an ownership check that fails must
            # not extend the entry's TTL. A rehydration touches it explicitly.
            return await store.peek(session_id)
        except Exception:
            logger.warning(
                "MCP session lookup for %s failed; falling through to 404",
                _short(session_id),
                exc_info=True,
            )
            return None

    @staticmethod
    def _metadata_from_scope(scope: Scope) -> dict[str, object]:
        """Extract the bits we can safely pull from the ASGI scope at create time.

        The owner (``api_key_id`` / ``user_id``) is added by the caller
        after it has been verified against the database.
        """
        meta: dict[str, object] = {"transport": "streamable_http"}
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
        ``StreamableHTTPSessionManager._handle_stateful_request`` (session
        ownership check, session limit, discard on DELETE) but adds three
        hooks:

        * persist new session ids to Redis
        * touch known session ids on every hit
        * rehydrate a fresh transport when the in-memory dict does not know
          a session id but Redis does
        """
        request = Request(scope, receive)
        request_mcp_session_id = request.headers.get(MCP_SESSION_ID_HEADER)

        user = scope.get("user")
        requestor = authorization_context(user) if isinstance(user, AuthenticatedUser) else None

        # ---- Existing in-memory session ---------------------------------
        if request_mcp_session_id is not None and request_mcp_session_id in self._server_instances:
            transport = self._server_instances[request_mcp_session_id]
            if requestor is None or requestor != self._session_owners.get(request_mcp_session_id):
                # A session can only be used with the credential that created
                # it. Respond exactly as if the session did not exist.
                logger.warning(
                    "Rejecting request for MCP session %s: credential does not match its owner",
                    _short(request_mcp_session_id),
                )
                await _session_not_found()(scope, receive, send)
                return
            logger.debug("Session already exists, handling request directly")
            # Refresh sliding TTL in the background; do not block the hot path.
            if self._task_group is not None:
                self._task_group.start_soon(self._touch_session, request_mcp_session_id)
            await transport.handle_request(scope, receive, send)
            if transport.is_terminated:
                # The client ended the session (DELETE): forget it now rather
                # than when its server task winds down.
                await self._discard_session(request_mcp_session_id, transport)
            return

        # ---- Brand-new session (no header at all) -----------------------
        if request_mcp_session_id is None:
            await self._create_and_run_session(scope, receive, send, session_id=None, requestor=requestor)
            return

        # ---- Header present but unknown to in-memory dict ---------------
        # Ask Redis before giving up. This is the restart-survival path.
        persisted = await self._lookup_session(request_mcp_session_id)
        if persisted is not None and await self._owns_persisted_session(persisted, scope, requestor):
            logger.info(
                "Rehydrating MCP session %s from Redis after in-memory miss",
                _short(request_mcp_session_id),
            )
            await self._create_and_run_session(
                scope,
                receive,
                send,
                session_id=request_mcp_session_id,
                requestor=requestor,
            )
            return

        # ---- Genuinely unknown — mirror upstream 404 --------------------
        await _session_not_found()(scope, receive, send)

    async def _owns_persisted_session(
        self,
        persisted: dict[str, str],
        scope: Scope,
        requestor: AuthorizationContext | None,
    ) -> bool:
        """Whether the request's key owns a session persisted by an earlier process.

        The key is re-checked against the database here, so a revoked or
        expired key, or a deactivated user, cannot resume a session. Entries
        without a recorded owner (written before owners were stored) are
        refused: the client simply opens a new session.
        """
        if requestor is None:
            logger.warning("Refusing MCP session rehydration: unauthenticated request")
            return False
        stored_key, stored_user = persisted.get("api_key_id"), persisted.get("user_id")
        if not stored_key or not stored_user:
            logger.warning("Refusing MCP session rehydration: the stored session has no owner")
            return False
        owner = await self._resolve_owner(scope)
        if owner is None or (str(owner[0]), str(owner[1])) != (stored_key, stored_user):
            logger.warning("Refusing MCP session rehydration: credential does not match the session owner")
            return False
        return True

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
        requestor: AuthorizationContext | None,
    ) -> None:
        """Create a fresh transport (either brand new or rehydrated).

        If ``session_id`` is ``None`` a new UUID4 id is minted (brand-new
        session) and persisted to Redis. If ``session_id`` is provided, the
        transport is created under that id (rehydration) and the Redis entry
        is touched to refresh its TTL — the entry already exists because the
        caller looked it up.
        """
        transport = await self._start_session_transport(scope, session_id=session_id, requestor=requestor)
        if transport is None:
            logger.warning("Refusing to open a new session: %d sessions are already open", self.max_sessions)
            response = Response(
                content=JSONRPCError(
                    jsonrpc="2.0",
                    id=None,
                    error=ErrorData(code=INVALID_REQUEST, message="Too many open sessions"),
                ).model_dump_json(by_alias=True, exclude_unset=True),
                status_code=HTTPStatus.SERVICE_UNAVAILABLE,
                media_type="application/json",
            )
            await response(scope, receive, send)
            return
        # Outside the lock: handling the request can take arbitrarily long. A GET
        # opens an SSE stream that lives as long as the client stays connected,
        # so holding the creation lock here would stall every other session —
        # after a restart the first client to reconnect would lock out all the
        # rest indefinitely.
        await transport.handle_request(scope, receive, send)

    async def _start_session_transport(
        self,
        scope: Scope,
        *,
        session_id: str | None,
        requestor: AuthorizationContext | None,
    ) -> StreamableHTTPServerTransport | None:
        """Create and start the transport for a session, returning it.

        Returns ``None`` when ``max_sessions`` sessions are already open.
        Holds ``_session_creation_lock`` only for the bookkeeping that needs it:
        the ``_server_instances`` mutation and the task-group start, both of
        which complete promptly. Serving the request is the caller's job.
        """
        from uuid import uuid4

        is_rehydration = session_id is not None
        assert self._task_group is not None
        headers = dict(scope.get("headers") or [])
        raw_version = headers.get(MCP_PROTOCOL_VERSION_HEADER.encode("ascii"))
        protocol_version = raw_version.decode("latin-1") if raw_version else None

        async with self._session_creation_lock:
            # Race: another coroutine may have just rehydrated/created the
            # same session id while we were waiting on the lock.
            if session_id is not None and session_id in self._server_instances:
                return self._server_instances[session_id]

            if self.max_sessions is not None and len(self._server_instances) >= self.max_sessions:
                return None

            new_session_id = session_id or uuid4().hex
            http_transport = StreamableHTTPServerTransport(
                mcp_session_id=new_session_id,
                is_json_response_enabled=self.json_response,
                event_store=self.event_store,
                security_settings=self.security_settings,
                retry_interval=self.retry_interval,
                idle_timeout=self.session_idle_timeout,
            )
            assert http_transport.mcp_session_id is not None
            self._server_instances[new_session_id] = http_transport
            if requestor is not None:
                self._session_owners[new_session_id] = requestor

            if is_rehydration:
                logger.info(
                    "Created rehydrated transport under existing session id %s",
                    _short(new_session_id),
                )
            else:
                logger.info("Created new transport with session ID: %s", _short(new_session_id))

            async def run_server(*, task_status: TaskStatus[None] = anyio.TASK_STATUS_IGNORED) -> None:
                async with http_transport.connect() as streams:
                    read_stream, write_stream = streams
                    task_status.started()
                    try:
                        # The transport cancels its idle scope once no request
                        # has been in flight for ``session_idle_timeout``; that
                        # ends the loop. The Redis entry survives, so the
                        # client's next request rehydrates the session.
                        idle_scope = getattr(http_transport, "idle_scope", None)
                        if idle_scope is None:
                            idle_scope = anyio.CancelScope()
                        with idle_scope:
                            await _serve_session(
                                self.app,
                                read_stream,
                                write_stream,
                                lifespan_state=self._lifespan_state,
                                session_id=new_session_id,
                                rehydrated=is_rehydration,
                                protocol_version=protocol_version,
                            )
                        if idle_scope.cancelled_caught:
                            logger.info("Session %s idle timeout", _short(new_session_id))
                    except Exception as e:
                        logger.error(
                            "Session %s crashed: %s",
                            _short(new_session_id),
                            e,
                            exc_info=True,
                        )
                    finally:
                        await self._discard_session(new_session_id, http_transport)

            await self._task_group.start(run_server)

            # Persist new sessions; for rehydration the entry already exists,
            # so just refresh the sliding TTL.
            if is_rehydration:
                await self._touch_session(new_session_id)
            else:
                await self._persist_new_session(new_session_id, scope)

            return http_transport
