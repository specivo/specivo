"""Pure ASGI middleware for request ID propagation, security headers, and audit batching.

Uses a raw ASGI middleware instead of BaseHTTPMiddleware to avoid
the extra task group that BaseHTTPMiddleware creates, which causes
"Future attached to a different loop" errors with asyncpg in tests.
"""

from __future__ import annotations

import contextvars
import dataclasses
import logging
import time
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "x-request-id"

# Security headers added to every HTTP response
_SECURITY_HEADERS: list[tuple[bytes, bytes]] = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"x-xss-protection", b"1; mode=block"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
    (b"permissions-policy", b"geolocation=(), microphone=(), camera=()"),
    (b"x-robots-tag", b"noindex, nofollow, noarchive"),
    # NOTE: 'unsafe-eval' is required by Alpine.js v3 standard build
    # (static/vendor/alpine.3.14.min.js) for x-model, x-bind expressions etc.
    # The CSP-safe build (alpine.csp.min.js) would remove this need but
    # disallows all inline expressions which breaks simple inline x-data
    # blocks like { expanded: false }.
    # TODO: Switch to Alpine.js CSP build (alpine.csp.3.14.min.js) to allow
    # removing both 'unsafe-eval' and 'unsafe-inline' from script-src. This
    # requires rewriting inline x-data blocks to use Alpine.data() instead.
    (
        b"content-security-policy",
        b"default-src 'self'; script-src 'self' 'unsafe-eval'; style-src 'self' 'unsafe-inline';"
        b" img-src 'self' data:; font-src 'self'; frame-ancestors 'none'",
    ),
]


class RateLimitHeaderMiddleware:
    """Inject rate limit headers stored on ``request.state`` into every response.

    The rate limit dependency stores headers on ``scope["state"]["rate_limit_headers"]``
    but endpoints that return their own ``Response`` (e.g. ``JSONResponse``) discard
    the injected ``Response`` object and its headers. This middleware copies the
    headers at the ASGI level so they always reach the client.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_rate_limit_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                state = scope.get("state", {})
                rl_headers: dict[str, str] | None = state.get("rate_limit_headers")
                if rl_headers:
                    resp_headers = list(message.get("headers", []))
                    # Collect existing header names to avoid duplicates
                    existing = {h[0].lower() for h in resp_headers}
                    for name, value in rl_headers.items():
                        if name.lower().encode() not in existing:
                            resp_headers.append((name.lower().encode(), value.encode()))
                    message["headers"] = resp_headers
            await send(message)

        await self.app(scope, receive, send_with_rate_limit_headers)


class RequestIDMiddleware:
    """Attach a unique request ID and security headers to every HTTP response.

    If the client sends ``X-Request-ID`` that value is reused; otherwise a
    UUID4 is generated. The ID is stored on ``scope["state"]["request_id"]``
    and echoed back via the ``X-Request-ID`` response header.

    Security headers (``X-Content-Type-Options``, ``X-Frame-Options``, etc.)
    are appended to every response to harden the API against common web
    vulnerabilities.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        # Extract request ID from incoming headers or generate one
        headers = dict(scope.get("headers", []))
        request_id = (headers.get(b"x-request-id") or b"").decode() or str(uuid.uuid4())

        # Store in scope state (accessible via request.state.request_id)
        scope.setdefault("state", {})
        scope["state"]["request_id"] = request_id

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                resp_headers = list(message.get("headers", []))
                resp_headers.append((b"x-request-id", request_id.encode()))
                resp_headers.extend(_SECURITY_HEADERS)
                message["headers"] = resp_headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


class LocaleMiddleware:
    """Set the active locale for each request based on user preference, cookie, or Accept-Language.

    Resolution order:
    1. Authenticated user's language preference (if available).
    2. ``specivo_lang`` cookie.
    3. ``Accept-Language`` header (best match against available languages).
    4. ``settings.default_language`` fallback.

    Activates the locale before the request and deactivates after.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        from specivo.core.config import get_settings
        from specivo.core.i18n import activate, deactivate

        settings = get_settings()
        locale = self._detect_locale(scope, settings)

        activate(locale)
        try:
            await self.app(scope, receive, send)
        finally:
            deactivate()

    def _detect_locale(self, scope: Scope, settings: object) -> str:
        """Determine the best locale for this request."""
        available = getattr(settings, "available_languages", ["en"])
        default = getattr(settings, "default_language", "en")
        headers = dict(scope.get("headers", []))

        # 1. Check specivo_lang cookie
        cookie_header = headers.get(b"cookie", b"").decode()
        if cookie_header:
            for part in cookie_header.split(";"):
                part = part.strip()
                if part.startswith("specivo_lang="):
                    lang = part.split("=", 1)[1].strip()
                    if lang in available:
                        return lang

        # 2. Parse Accept-Language header
        accept_lang = headers.get(b"accept-language", b"").decode()
        if accept_lang:
            best = self._parse_accept_language(accept_lang, available)
            if best:
                return best

        return default

    @staticmethod
    def _parse_accept_language(header: str, available: list[str]) -> str | None:
        """Parse Accept-Language header and return the best match."""
        # Simple parser: split by comma, extract lang and quality
        candidates: list[tuple[float, str]] = []
        for part in header.split(","):
            part = part.strip()
            if not part:
                continue
            if ";q=" in part:
                lang, q_str = part.split(";q=", 1)
                try:
                    q = float(q_str.strip())
                except ValueError:
                    q = 0.0
            else:
                lang = part
                q = 1.0
            lang = lang.strip().lower()
            candidates.append((q, lang))

        # Sort by quality descending
        candidates.sort(key=lambda x: x[0], reverse=True)

        for _, lang in candidates:
            # Exact match
            if lang in available:
                return lang
            # Language prefix match (e.g. "en-us" -> "en")
            prefix = lang.split("-")[0]
            if prefix in available:
                return prefix

        return None


class AuditBatchMiddleware:
    """Collect audit events during a request and flush them in a single batch INSERT.

    On request start, initialises ``scope["state"]["audit_events"]`` as an empty list.
    Endpoint code (via SecurityAuditService) appends event dicts to this list instead
    of issuing individual INSERTs.

    After the response is produced, the middleware flushes all collected events in a
    single DB round-trip using an independent session — but **only** when the
    ``security_audit_log`` feature is available (i.e. the enterprise plugin is loaded).
    Without enterprise, events are silently discarded.

    Errors during flush are logged as warnings but never fail the response.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Initialise the per-request audit event buffer
        scope.setdefault("state", {})
        scope["state"]["audit_events"] = []

        await self.app(scope, receive, send)

        # Flush collected audit events after the response — only when
        # the enterprise plugin provides the security_audit_log feature.
        events = scope["state"].get("audit_events", [])
        if events and self._enterprise_audit_enabled():
            await self._flush_events(events)

    @staticmethod
    def _enterprise_audit_enabled() -> bool:
        """Return True if the security_audit_log feature is registered."""
        try:
            from specivo.core.features import has_feature

            return has_feature("security_audit_log")
        except (RuntimeError, ImportError):
            return False

    async def _flush_events(self, events: list[dict]) -> None:
        """Batch INSERT all collected audit events using an independent session."""
        try:
            from specivo.core.database import get_session_factory
            from specivo.models.security_audit import SecurityAuditLog

            factory = get_session_factory()
            async with factory() as session:
                for event_data in events:
                    log = SecurityAuditLog(**event_data)
                    session.add(log)
                await session.commit()
        except Exception:
            logger.warning("Batch audit flush failed", exc_info=True)


class TokenRefreshMiddleware:
    """Set auth cookies when a silent token refresh occurred during the request.

    ``get_current_user_optional()`` in the web layer stores new tokens on
    ``scope["state"]["refreshed_tokens"]`` when it transparently rotates an
    expired access token using the refresh token cookie.  This middleware
    intercepts the ``http.response.start`` message and appends ``Set-Cookie``
    headers for both ``access_token`` and ``refresh_token``.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_refresh_cookies(message: Message) -> None:
            if message["type"] == "http.response.start":
                state = scope.get("state", {})
                tokens: dict[str, str] | None = state.get("refreshed_tokens")
                if tokens:
                    from specivo.core.config import get_settings

                    settings = get_settings()
                    resp_headers = list(message.get("headers", []))
                    resp_headers.extend(
                        _build_auth_cookie_headers(
                            access_token=tokens["access_token"],
                            refresh_token=tokens["refresh_token"],
                            settings=settings,
                            remember=tokens.get("remember", True),
                        )
                    )
                    message["headers"] = resp_headers
            await send(message)

        await self.app(scope, receive, send_with_refresh_cookies)


def _build_auth_cookie_headers(
    access_token: str,
    refresh_token: str,
    settings: object,
    *,
    remember: bool = True,
) -> list[tuple[bytes, bytes]]:
    """Build raw ``Set-Cookie`` header tuples for the auth cookies.

    Mirrors the cookie attributes in ``specivo.api.v1.auth._set_auth_cookies``
    but produces raw ASGI header tuples for use in middleware.

    When *remember* is ``False``, ``Max-Age`` is omitted so the browser
    treats the cookies as session cookies.
    """
    debug = getattr(settings, "debug", False)
    secure_flag = "" if debug else "; Secure"

    max_age_access = ""
    max_age_refresh = ""
    if remember:
        access_max_age = getattr(settings, "access_token_expire_minutes", 15) * 60
        refresh_max_age = getattr(settings, "refresh_token_expire_days", 30) * 86400
        max_age_access = f"; Max-Age={access_max_age}"
        max_age_refresh = f"; Max-Age={refresh_max_age}"

    access_cookie = f"access_token={access_token}; HttpOnly; SameSite=Lax; Path=/{max_age_access}{secure_flag}"
    refresh_cookie = f"refresh_token={refresh_token}; HttpOnly; SameSite=Lax; Path=/{max_age_refresh}{secure_flag}"
    return [
        (b"set-cookie", access_cookie.encode()),
        (b"set-cookie", refresh_cookie.encode()),
    ]


# ---------------------------------------------------------------------------
# SQL Debug Profiler (active only when settings.debug=True)
# ---------------------------------------------------------------------------

_sql_debug_logger = logging.getLogger("sql_debug")


@dataclasses.dataclass
class _QueryRecord:
    sql: str
    duration_ms: float = 0.0
    _start: float = 0.0


_sql_debug_queries: contextvars.ContextVar[list[_QueryRecord] | None] = contextvars.ContextVar(
    "_sql_debug_queries", default=None
)


def install_sql_debug_hooks(engine: object) -> None:
    """Attach before/after cursor-execute listeners for per-request SQL profiling.

    *engine* must be a :class:`~sqlalchemy.ext.asyncio.AsyncEngine`.  The hooks
    are installed on its underlying ``sync_engine`` so they fire inside the
    thread that actually executes the DBAPI call.
    """
    from sqlalchemy import event as _event

    sync_engine = engine.sync_engine  # type: ignore[attr-defined]

    @_event.listens_for(sync_engine, "before_cursor_execute")
    def _before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        queries = _sql_debug_queries.get()
        if queries is None:
            return
        record = _QueryRecord(sql=statement, _start=time.perf_counter())
        queries.append(record)

    @_event.listens_for(sync_engine, "after_cursor_execute")
    def _after_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        queries = _sql_debug_queries.get()
        if queries is None or not queries:
            return
        record = queries[-1]
        record.duration_ms = (time.perf_counter() - record._start) * 1000.0


class SQLDebugMiddleware:
    """Per-request SQL query profiler that adds debug headers and logs query details.

    Only useful when ``settings.debug=True``; the middleware itself does not
    check the flag — the caller decides whether to register it.

    Headers injected into every response:

    * ``X-SQL-Query-Count`` — number of SQL statements executed.
    * ``X-SQL-Time-Ms`` — cumulative SQL wall-clock time in milliseconds.
    * ``X-Request-Time-Ms`` — total request wall-clock time in milliseconds.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        queries: list[_QueryRecord] = []
        token = _sql_debug_queries.set(queries)
        request_start = time.perf_counter()

        # Expose to templates via request.state.sql_debug
        scope.setdefault("state", {})
        scope["state"]["sql_debug_queries"] = queries
        scope["state"]["sql_debug_start"] = request_start

        try:

            async def send_with_debug_headers(message: Message) -> None:
                if message["type"] == "http.response.start":
                    request_ms = (time.perf_counter() - request_start) * 1000.0
                    sql_ms = sum(q.duration_ms for q in queries)
                    resp_headers = list(message.get("headers", []))
                    resp_headers.append((b"x-sql-query-count", str(len(queries)).encode()))
                    resp_headers.append((b"x-sql-time-ms", f"{sql_ms:.1f}".encode()))
                    resp_headers.append((b"x-request-time-ms", f"{request_ms:.1f}".encode()))
                    message["headers"] = resp_headers
                await send(message)

            await self.app(scope, receive, send_with_debug_headers)

            # Log query summary (only when queries were actually executed)
            if queries:
                request_ms = (time.perf_counter() - request_start) * 1000.0
                sql_ms = sum(q.duration_ms for q in queries)
                method = scope.get("method", "?")
                path = scope.get("path", "?")

                lines = [
                    f"{method} {path}",
                    f"Queries: {len(queries)} | Total: {request_ms:.1f}ms | SQL: {sql_ms:.1f}ms",
                ]
                for idx, q in enumerate(queries, 1):
                    truncated = q.sql[:200]
                    lines.append(f"  [{idx}] {q.duration_ms:.1f}ms  {truncated}")
                _sql_debug_logger.debug("\n".join(lines))
        finally:
            _sql_debug_queries.reset(token)
