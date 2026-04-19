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
"""

from __future__ import annotations

import logging

import jwt
from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.core.constants import API_KEY_PREFIX, JWT_ALGORITHM
from specivo.core.database import get_db
from specivo.core.exceptions import AppError
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

        return user

    try:
        return await _authenticate_jwt(token, db)
    except AppError as exc:
        # Silent refresh for cookie-based sessions when the JWT is
        # present but expired.  Never invoked on the Authorization:
        # Bearer path — those callers manage their own tokens.
        if from_cookie and exc.code == "auth_token_expired":
            refreshed = await try_silent_refresh(request, db)
            if refreshed is not None:
                return refreshed
        await _log_auth_failure(db, "invalid_jwt", request)
        raise


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
