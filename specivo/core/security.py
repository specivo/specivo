"""Authentication dependencies: JWT + API key resolution.

This module provides:
- ``get_current_user``: FastAPI dependency that resolves the authenticated user
  from either a JWT access token or an API key.
- ``blocklist_token`` / ``is_token_blocked``: Redis JWT blocklist helpers.

Resolution order in ``get_current_user``:
1. ``Authorization: Bearer <token>`` header
   - Starts with ``spv_`` → API key auth
   - Otherwise → JWT auth
2. ``access_token`` cookie → JWT auth
3. Neither → 401 Unauthorized

JWT validation steps (order matters):
1. Decode + verify signature and expiry (PyJWT, HS256)
2. Check Redis blocklist (``jwt_blocklist:{jti}``) — gracefully degraded if Redis is down
3. Load user from DB by ``sub`` claim
4. Check ``user.status`` — only ``active`` and ``locked`` pass (locked blocks JWT per spec;
   the locked check is explicit below to return a meaningful error code)

API key validation delegates to ``ApiKeyService.authenticate``, which:
- Allows locked users (locking is brute-force protection, not agent access control)
- Blocks deactivated users

Forced password change: a user carrying ``must_change_password`` is refused every
API path except the handful listed in ``PASSWORD_CHANGE_EXEMPT_SUFFIXES``. The
check runs on the JWT and cookie paths only — an agent holding a valid API key
must never start failing because of a flag on a row it does not own. The web
layer does not use this refusal at all:
``specivo.web.deps.get_current_user_optional`` resolves the same user through
``authenticate_request(..., enforce_password_change=False)`` and redirects it to
the change-password page instead.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager

import jwt
from fastapi import Depends, Request, Response
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.core.constants import API_KEY_PREFIX, JWT_ALGORITHM
from specivo.core.database import get_db
from specivo.core.exceptions import AnonymousAccessDeniedError, AppError
from specivo.core.middleware import ANONYMOUS_READER_STATE_KEY
from specivo.core.rate_limit import enforce_rate_limit
from specivo.core.utils import utcnow
from specivo.models.user import User
from specivo.services.agent_session_service import AgentSessionService
from specivo.services.api_key_service import ApiKeyService

logger = logging.getLogger(__name__)

_api_key_service = ApiKeyService()
_agent_session_service = AgentSessionService()

# ---------------------------------------------------------------------------
# Redis JWT blocklist
# ---------------------------------------------------------------------------

_BLOCKLIST_PREFIX = "jwt_blocklist:"


async def blocklist_token(jti: str, ttl_seconds: int) -> None:
    """Add a JWT ID to the Redis blocklist with the given TTL.

    Silent no-op if Redis is unavailable (logs a warning).
    """
    if ttl_seconds <= 0:
        return
    try:
        from specivo.core.redis import get_redis

        redis = await get_redis()
        await redis.setex(f"{_BLOCKLIST_PREFIX}{jti}", ttl_seconds, "1")
    except Exception as exc:
        logger.warning("Redis blocklist write failed (jti=%s): %s", jti, exc)


async def is_token_blocked(jti: str) -> bool:
    """Return True if the JWT ID is in the Redis blocklist.

    Returns True (blocked / deny) if Redis is unavailable.  A security
    mechanism that silently degrades to "allow everything" defeats its
    purpose.  API key auth (which does not use the Redis blocklist)
    remains available as a fallback for agents and CI.
    """
    try:
        from specivo.core.redis import get_redis

        redis = await get_redis()
        return await redis.exists(f"{_BLOCKLIST_PREFIX}{jti}") > 0
    except Exception as exc:
        logger.warning("Redis blocklist unavailable — denying JWT auth (fail-closed): %s", exc)
        return True


# ---------------------------------------------------------------------------
# Forced password change
# ---------------------------------------------------------------------------

# What a user with ``must_change_password`` may still reach. Entries are path
# suffixes matched after the configured stealth prefix, built the same way
# ``CSRFMiddleware`` builds its own exempt list.
#
# - change-password is the way out, so it has to be reachable.
# - logout and logout-all: leaving must always be possible. An account somebody
#   else set a password on is exactly the account whose owner may want out.
# - refresh: a browser sitting on the change-password page long enough for its
#   access token to expire would otherwise lose the session mid-flow, and the
#   silent refresh runs through this dependency.
# - health: it carries no auth dependency today, so nothing here can reach it.
#   It is listed anyway because a liveness probe must never depend on the state
#   of whichever account happens to be calling.
PASSWORD_CHANGE_EXEMPT_SUFFIXES = (
    "/api/v1/auth/change-password/",
    "/api/v1/auth/logout/",
    "/api/v1/auth/logout-all/",
    "/api/v1/auth/refresh/",
    "/health/",
)


def is_password_change_exempt(path: str) -> bool:
    """Return True if *path* stays reachable while a password change is forced."""
    sp = get_settings().stealth_prefix.rstrip("/")
    return any(path.startswith(sp + suffix) for suffix in PASSWORD_CHANGE_EXEMPT_SUFFIXES)


def _enforce_password_change(user: User, request: Request) -> None:
    """Refuse *user* unless the request is for one of the exempt paths.

    Only ever called on the JWT and cookie paths. API key authentication is
    deliberately left alone: an agent's access must not depend on a flag set
    for the benefit of whoever signs in with a password.
    """
    if not user.must_change_password:
        return
    if is_password_change_exempt(request.url.path):
        return
    raise AppError(
        code="password_change_required",
        message=(
            "This account must set a new password before it can be used. "
            "POST the current and new password to /api/v1/auth/change-password/."
        ),
        status_code=403,
    )


# ---------------------------------------------------------------------------
# JWT decoding helper
# ---------------------------------------------------------------------------


async def _authenticate_jwt(token: str, db: AsyncSession) -> User:
    """Validate a JWT access token and return the associated User.

    Raises ``AppError(401)`` on any validation failure.
    """
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise AppError(
            code="auth_token_expired",
            message="Access token has expired",
            status_code=401,
        )
    except jwt.InvalidTokenError:
        raise AppError(
            code="auth_token_invalid",
            message="Invalid access token",
            status_code=401,
        )

    # Check Redis blocklist (gracefully degraded)
    jti = payload.get("jti")
    if jti and await is_token_blocked(jti):
        raise AppError(
            code="auth_token_revoked",
            message="Access token has been revoked",
            status_code=401,
        )

    # Load user from DB (sub is stored as string, convert to int)
    sub = payload.get("sub")
    if not sub:
        raise AppError(
            code="auth_token_invalid",
            message="Token missing subject claim",
            status_code=401,
        )
    try:
        user_id = int(sub)
    except (ValueError, TypeError):
        raise AppError(code="auth_token_invalid", message="Invalid subject claim", status_code=401)

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if user is None:
        raise AppError(
            code="auth_token_invalid",
            message="User not found",
            status_code=401,
        )

    # Nobody signs in as the anonymous user, so no genuine token names it.
    # Refused explicitly rather than through its deactivated status below,
    # which this check must not depend on.
    if user.is_anonymous:
        raise AppError(
            code="auth_token_invalid",
            message="Invalid access token",
            status_code=401,
        )

    # Locked accounts cannot use JWT auth (per spec: locking blocks JWT, not API keys)
    if user.status == "locked":
        raise AppError(
            code="auth_account_locked",
            message="Account is locked. Contact an administrator.",
            status_code=401,
        )

    if user.status != "active":
        _status_codes = {
            "deactivated": "auth_account_deactivated",
            "pending_verification": "auth_email_not_verified",
        }
        code = _status_codes.get(user.status, "auth_token_invalid")
        raise AppError(
            code=code,
            message=f"Account is not active (status: {user.status})",
            status_code=401,
        )

    return user


# ---------------------------------------------------------------------------
# Silent refresh helper (shared between get_current_user and
# get_current_user_optional)
# ---------------------------------------------------------------------------


async def try_silent_refresh(request: Request, db: AsyncSession) -> User | None:
    """Attempt a silent JWT refresh using the ``refresh_token`` cookie.

    Returns the refreshed ``User`` and stores the new tokens on
    ``request.state.refreshed_tokens`` so ``TokenRefreshMiddleware`` can
    attach ``Set-Cookie`` headers to the outgoing response.

    Returns ``None`` when:
    - no ``refresh_token`` cookie is present
    - the refresh token is expired/invalid/revoked
    - the underlying ``AuthService.refresh`` call fails for any reason

    This helper is only meaningful for cookie-based sessions.  Callers on
    the ``Authorization: Bearer`` path (JWT or API key) should not invoke
    it — those clients manage their own tokens.
    """
    refresh_token_raw = request.cookies.get("refresh_token")
    if not refresh_token_raw:
        return None

    try:
        from specivo.core.config import get_settings
        from specivo.services.auth_service import AuthService

        settings = get_settings()

        # Carry forward the "remember me" preference from the (expired)
        # access token if we can still decode it without verifying exp.
        remember = True
        old_access = request.cookies.get("access_token")
        if old_access:
            try:
                payload = jwt.decode(
                    old_access,
                    settings.secret_key,
                    algorithms=[JWT_ALGORITHM],
                    options={"verify_exp": False},
                )
                remember = payload.get("rem", True)
            except Exception:
                pass

        svc = AuthService()
        access_token, new_refresh_token, refreshed_user = await svc.refresh(
            session=db,
            refresh_token_raw=refresh_token_raw,
            remember=remember,
        )

        request.state.refreshed_tokens = {
            "access_token": access_token,
            "refresh_token": new_refresh_token,
            "remember": remember,
        }
        return refreshed_user
    except Exception:
        logger.debug("Silent token refresh failed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Main dependency
# ---------------------------------------------------------------------------


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> User:
    """FastAPI dependency: resolve the current user from JWT or API key.

    Thin wrapper over :func:`authenticate_request` with the forced
    password-change gate on, which is what every JSON API endpoint wants.
    """
    return await authenticate_request(request, db)


async def authenticate_request(
    request: Request,
    db: AsyncSession,
    *,
    enforce_password_change: bool = True,
) -> User:
    """Resolve the current user from JWT or API key.

    *enforce_password_change* is switched off by the web layer, which needs the
    resolved user in order to redirect it to the change-password page; a 403
    raised here would reach ``get_current_user_optional`` as "not signed in"
    and send a forced user to the login page instead.

    Resolution order:
    1. ``Authorization: Bearer <token>`` header
       - Starts with ``spv_`` → API key auth
       - Otherwise → JWT auth
    2. ``access_token`` cookie → JWT auth
       - If the cookie is missing or the JWT has expired, attempt a silent
         refresh using the ``refresh_token`` cookie.
    3. Neither → 401 Unauthorized

    Returns the authenticated ``User`` model instance.
    Raises ``AppError(401)`` on any auth failure.
    """
    auth_header = request.headers.get("Authorization", "")
    token: str | None = None
    use_api_key = False
    from_cookie = False

    if auth_header.startswith("Bearer "):
        token = auth_header[len("Bearer ") :]
        use_api_key = token.startswith(API_KEY_PREFIX)
    else:
        # Fall back to cookie
        token = request.cookies.get("access_token")
        from_cookie = True

    if not token:
        # Cookie-based sessions may still recover via silent refresh
        # (browser dropped the short-lived access_token cookie but kept
        # the long-lived refresh_token).
        if from_cookie:
            refreshed = await try_silent_refresh(request, db)
            if refreshed is not None:
                if enforce_password_change:
                    _enforce_password_change(refreshed, request)
                return refreshed
        await _log_auth_failure(db, "no_credentials", request)
        raise AppError(
            code="unauthorized",
            message="Authentication required",
            status_code=401,
        )

    if use_api_key:
        client_ip = request.client.host if request.client else None
        user, api_key = await _api_key_service.authenticate(
            session=db,
            raw_key=token,
            client_ip=client_ip,
        )
        # No API key can name the anonymous row: it has no password, it is
        # deactivated, and ApiKeyService refuses deactivated users. Refused
        # again here so that "get_current_user never returns the anonymous
        # principal" does not quietly depend on all three staying true.
        if user.is_anonymous:
            raise AppError(code="api_key_invalid", message="Invalid API key", status_code=401)

        # Store API key scopes on request.state for downstream permission checks
        request.state.api_key_scopes = api_key.scopes
        request.state.api_key_id = api_key.id

        # Auto-create/update agent session for API key usage
        try:
            user_agent = request.headers.get("User-Agent")
            await _agent_session_service.get_or_create_session(
                session=db,
                api_key_id=api_key.id,
                user_id=user.id,
                user_agent=user_agent,
            )
        except Exception as exc:
            # Agent session tracking is non-critical — never block auth
            logger.warning("Agent session tracking failed: %s", exc)

        # No forced password-change check here, deliberately: an API key is not
        # a password, and an agent holding a valid one must not start failing
        # because somebody flagged the row it authenticates as.
        return user

    try:
        user = await _authenticate_jwt(token, db)
    except AppError as exc:
        # Silent refresh for cookie-based sessions when the JWT is
        # present but expired.  Never invoked on the Authorization:
        # Bearer path — those callers manage their own tokens.
        if from_cookie and exc.code == "auth_token_expired":
            refreshed = await try_silent_refresh(request, db)
            if refreshed is not None:
                if enforce_password_change:
                    _enforce_password_change(refreshed, request)
                return refreshed
        await _log_auth_failure(db, "invalid_jwt", request)
        raise

    if enforce_password_change:
        _enforce_password_change(user, request)
    return user


# ---------------------------------------------------------------------------
# Reader dependency: the one place the anonymous principal enters a request
# ---------------------------------------------------------------------------

# The GET routes that may be served to a visitor without an account, as the
# path templates FastAPI registers them under (before any stealth prefix).
#
# This is the whole surface anonymous access has. A route joins it by being
# written here *and* by depending on ``get_reader``; the contract test in
# ``tests/integration/test_anonymous_route_contract.py`` fails when the two
# disagree, so a new route cannot drift into the set by accident, and one
# listed here cannot quietly stop being served.
ANONYMOUS_READ_ROUTES: frozenset[str] = frozenset(
    {
        "/api/v1/projects/",
        "/api/v1/projects/{project_key}/issues/",
        "/api/v1/issues/{issue_ref}/",
        "/api/v1/projects/{project_key}/wiki/{slug}/",
        "/api/v1/search/",
    }
)

# Per-IP budgets for anonymous traffic, kept in their own Redis buckets so a
# crawler cannot spend a signed-in user's allowance or vice versa.
ANONYMOUS_READ_RATE_LIMIT = (120, 60)
ANONYMOUS_SEARCH_RATE_LIMIT = (10, 60)

# How long a single statement may run for an anonymous visitor. Every query on
# the allowlisted routes is an indexed read that finishes far inside this; the
# limit is here so a crafted search cannot hold a connection open.
ANONYMOUS_STATEMENT_TIMEOUT_MS = 2000

# What a visitor without an account may ask of search. Keyword only: semantic
# and hybrid both run an embedding model, and that is cost a stranger must not
# be able to spend. The page is short and close to the surface so the result
# set cannot be walked wholesale.
#
# Declared here rather than beside either caller because both enforce them and
# they must not drift: the JSON API refuses a request outside these bounds,
# while the search *page* narrows one to them — a browser arrives with whatever
# the form put in the URL, and refusing the default mode would turn the page
# into a refusal for everyone.
ANONYMOUS_SEARCH_MODE = "keyword"
ANONYMOUS_SEARCH_MAX_LIMIT = 25
ANONYMOUS_SEARCH_MAX_OFFSET = 500


def has_credentials(request: Request) -> bool:
    """Return True if the request carries anything that claims to be a credential.

    Deliberately asks "did the caller try to authenticate", not "did it
    work". A request holding a revoked API key or an expired token must be
    answered with 401, never quietly downgraded to an anonymous read — an
    agent whose key was revoked would otherwise keep receiving public data
    and skip every scope check, and would have no way to notice.
    """
    if request.headers.get("authorization", "").strip():
        return True
    if request.headers.get("x-api-key", "").strip():
        return True
    return bool(request.cookies.get("access_token") or request.cookies.get("refresh_token"))


async def resolve_anonymous_principal(db: AsyncSession) -> User | None:
    """Return the anonymous user row, or None when it must not be used.

    None means "there is nothing to serve without an account": either the
    instance switch is off, or the row is missing because the database was
    never migrated. The two are deliberately not told apart — a stranger
    learns nothing from either.
    """
    from specivo.services.anonymous_access_service import is_anonymous_access_enabled
    from specivo.services.anonymous_user_service import get_anonymous_user

    if not await is_anonymous_access_enabled(db):
        return None
    return await get_anonymous_user(db)


@asynccontextmanager
async def anonymous_read_scope(
    request: Request,
    response: Response,
    db: AsyncSession,
) -> AsyncIterator[None]:
    """Run a block as an anonymous visitor: metered, flagged and read-only.

    Shared by the JSON API (:func:`get_reader`) and the web pages
    (``specivo.web.deps.get_web_reader``) so the two cannot drift. Everything
    an anonymous request is subject to lives here and nowhere else:

    - the scope-state flag that makes ``AnonymousResponseHeadersMiddleware``
      mark the response uncacheable;
    - the per-IP read budget, applied after the flag so that a 429 is marked
      uncacheable too;
    - a savepoint marked ``transaction_read_only`` with a short statement
      timeout, so a handler attempting a write fails loudly instead of being
      committed, and a crafted query cannot hold a connection open.

    The savepoint is rolled back on the way out; ``SET LOCAL`` is scoped to it
    either way, so neither the read-only flag nor the statement timeout escapes
    into the surrounding transaction — which is what lets the same code run
    under the test suite's outer transaction.
    """
    setattr(request.state, ANONYMOUS_READER_STATE_KEY, True)
    await enforce_rate_limit(request, response, "anon_read", *ANONYMOUS_READ_RATE_LIMIT)

    savepoint = await db.begin_nested()
    try:
        await db.execute(text("SET LOCAL transaction_read_only = on"))
        await db.execute(text(f"SET LOCAL statement_timeout = {ANONYMOUS_STATEMENT_TIMEOUT_MS}"))
        yield
    finally:
        if savepoint.is_active:
            await savepoint.rollback()


async def get_reader(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> AsyncGenerator[User, None]:
    """FastAPI dependency: the signed-in user, or the anonymous principal.

    Used only on the routes in :data:`ANONYMOUS_READ_ROUTES`. Three cases,
    and the middle one is the point of the whole dependency:

    - **Credentials present** — resolved by :func:`authenticate_request`,
      exactly as ``get_current_user`` would. Bad, expired or revoked
      credentials raise 401 from there and never reach the anonymous branch.
    - **No credentials at all** — the anonymous principal, but only while the
      instance switch is on. Otherwise ``AnonymousAccessDeniedError``, the
      same 401 every other anonymous refusal produces.
    - The anonymous row missing (an unmigrated database) is treated as the
      switch being off rather than as an error worth reporting to a stranger.

    An anonymous request then runs inside a savepoint marked
    ``transaction_read_only``, so any write a handler attempts fails loudly
    instead of being committed. The savepoint is rolled back on the way out;
    ``SET LOCAL`` is scoped to it either way, so neither the read-only flag
    nor the statement timeout escapes into the surrounding transaction — which
    is what lets the same code run under the test suite's outer transaction.
    """
    if has_credentials(request):
        yield await authenticate_request(request, db)
        return

    anonymous = await resolve_anonymous_principal(db)
    if anonymous is None:
        # Nothing to serve: either the switch is off, or the row is missing.
        # With the switch off this is just an unauthenticated request to a
        # protected route, which is what it was before this dependency
        # existed — so it is audited exactly as ``get_current_user`` audits
        # it, and turning the switch off leaves the route's behaviour
        # unchanged down to the audit trail. Refusals that happen *because*
        # of a project's settings, once the switch is on, are routine and are
        # deliberately not logged; see the route handlers.
        await _log_auth_failure(db, "no_credentials", request)
        raise AnonymousAccessDeniedError()

    async with anonymous_read_scope(request, response, db):
        yield anonymous


# ---------------------------------------------------------------------------
# Convenience helper: compute remaining TTL from a JWT exp claim
# ---------------------------------------------------------------------------


async def _log_auth_failure(db: AsyncSession, reason: str, request: Request) -> None:
    """Log an authentication failure to the security audit trail.

    Non-critical — swallows exceptions to never block auth flow.
    Uses batch mode (request.state.audit_events) when available so the
    AuditBatchMiddleware flushes the event in its own session after the
    response, surviving the outer transaction rollback caused by the 401 error.
    """
    try:
        from specivo.services.security_audit_service import AuditEvent, SecurityAuditService

        audit = SecurityAuditService()
        ip = request.client.host if request.client else None
        request_id = request.headers.get("x-request-id")
        await audit.log_event(
            session=db,
            event_type=AuditEvent.AUTH_FAILURE,
            user_id=None,
            ip_address=ip,
            request_id=request_id,
            details={"reason": reason},
            request=request,
        )
    except Exception:
        logger.warning("Security audit logging failed for auth failure", exc_info=True)


def token_remaining_ttl(exp: int) -> int:
    """Return the number of seconds until the token expires (minimum 0)."""
    remaining = exp - int(utcnow().timestamp())
    return max(0, remaining)
