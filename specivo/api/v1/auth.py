"""Auth endpoints: login, refresh, logout, session management.

Endpoints:
    POST   /auth/login           - Authenticate, return tokens + set cookies
    POST   /auth/refresh         - Rotate refresh token
    POST   /auth/logout          - Revoke one session (cookie-aware)
    POST   /auth/logout-all      - Revoke all sessions for the current user
    GET    /auth/sessions        - List active sessions
    DELETE /auth/sessions/{id}   - Revoke a specific session
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import get_db
from specivo.core.exceptions import AppError
from specivo.core.rate_limit import rate_limit
from specivo.core.security import get_current_user
from specivo.models.user import User
from specivo.schemas.auth import LoginRequest, RefreshRequest, SessionOut, TokenResponse
from specivo.services.auth_service import AuthService

router = APIRouter()
_service = AuthService()

# ---------------------------------------------------------------------------
# Cookie names and paths (mirror the wiki spec)
# ---------------------------------------------------------------------------
# NOTE: Using plain names ("access_token", "refresh_token") for backward
# compatibility with existing sessions. Future versions should consider the
# ``__Host-`` prefix (e.g. ``__Host-access_token``) which binds the cookie
# to the origin (requires Secure, Path=/, no Domain). Changing the name
# now would invalidate all active sessions.
_ACCESS_COOKIE = "access_token"
_REFRESH_COOKIE = "refresh_token"
_REFRESH_COOKIE_PATH = "/api/v1/auth"


def _set_auth_cookies(response: Response, access_token: str, refresh_token: str, settings) -> None:
    """Attach httpOnly auth cookies for browser clients."""
    response.set_cookie(
        key=_ACCESS_COOKIE,
        value=access_token,
        httponly=True,
        samesite="lax",
        secure=not settings.debug,
        path="/",
        max_age=settings.access_token_expire_minutes * 60,
    )
    response.set_cookie(
        key=_REFRESH_COOKIE,
        value=refresh_token,
        httponly=True,
        samesite="lax",
        secure=not settings.debug,
        path=_REFRESH_COOKIE_PATH,
        max_age=settings.refresh_token_expire_days * 86400,
    )


def _clear_auth_cookies(response: Response) -> None:
    response.delete_cookie(key=_ACCESS_COOKIE, path="/")
    response.delete_cookie(key=_REFRESH_COOKIE, path=_REFRESH_COOKIE_PATH)


# ---------------------------------------------------------------------------
# POST /auth/login
# ---------------------------------------------------------------------------


_login_rate_limit = rate_limit("auth_login", max_requests=10, window_seconds=60)
_refresh_rate_limit = rate_limit("auth_refresh", max_requests=30, window_seconds=60)


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Login with username/email and password",
    responses={
        401: {"description": "Invalid credentials or account locked"},
        429: {"description": "Rate limit exceeded"},
    },
)
async def login(
    body: LoginRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    _rl: Annotated[None, Depends(_login_rate_limit)],
) -> Response:
    from specivo.core.config import get_settings

    settings = get_settings()
    device_info = request.headers.get("User-Agent")
    ip = request.client.host if request.client else None

    access_token, refresh_token = await _service.login(
        session=db,
        login_or_email=body.login,
        password=body.password,
        device_info=device_info,
        ip=ip,
    )

    # Audit log the successful login
    try:
        import jwt as pyjwt

        from specivo.services.security_audit_service import SecurityAuditService

        payload = pyjwt.decode(access_token, settings.secret_key, algorithms=["HS256"])
        user_id = int(payload.get("sub", 0))
        audit = SecurityAuditService()
        await audit.log_event(
            session=db,
            event_type="login_success",
            user_id=user_id,
            ip_address=ip,
            details={"method": "password"},
        )
    except Exception:
        pass  # Non-critical — never block login

    token_data = TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=settings.access_token_expire_minutes * 60,
    )
    response = JSONResponse(content=token_data.model_dump(), status_code=status.HTTP_200_OK)
    _set_auth_cookies(response, access_token, refresh_token, settings)
    return response


# ---------------------------------------------------------------------------
# POST /auth/refresh
# ---------------------------------------------------------------------------


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Rotate refresh token and issue new access token",
    responses={401: {"description": "Refresh token expired or revoked"}},
)
async def refresh(
    db: Annotated[AsyncSession, Depends(get_db)],
    _rl: Annotated[None, Depends(_refresh_rate_limit)],
    body: RefreshRequest | None = None,
    refresh_token_cookie: Annotated[str | None, Cookie(alias=_REFRESH_COOKIE)] = None,
) -> Response:
    from specivo.core.config import get_settings

    settings = get_settings()

    # Prefer body token; fall back to cookie
    raw_token = (body.refresh_token if body else None) or refresh_token_cookie
    if not raw_token:
        raise AppError(
            code="auth_refresh_expired",
            message="Refresh token expired or revoked",
            status_code=401,
        )

    access_token, new_refresh_token = await _service.refresh(session=db, refresh_token_raw=raw_token)

    token_data = TokenResponse(
        access_token=access_token,
        refresh_token=new_refresh_token,
        expires_in=settings.access_token_expire_minutes * 60,
    )
    response = JSONResponse(content=token_data.model_dump(), status_code=status.HTTP_200_OK)
    _set_auth_cookies(response, access_token, new_refresh_token, settings)
    return response


# ---------------------------------------------------------------------------
# POST /auth/logout
# ---------------------------------------------------------------------------


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke the current refresh token",
)
async def logout(
    db: Annotated[AsyncSession, Depends(get_db)],
    body: RefreshRequest | None = None,
    refresh_token_cookie: Annotated[str | None, Cookie(alias=_REFRESH_COOKIE)] = None,
) -> Response:
    raw_token = (body.refresh_token if body else None) or refresh_token_cookie
    if raw_token:
        await _service.logout(session=db, refresh_token_raw=raw_token)

    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    _clear_auth_cookies(response)
    return response


# ---------------------------------------------------------------------------
# POST /auth/logout-all
# ---------------------------------------------------------------------------


@router.post(
    "/logout-all",
    summary="Revoke all sessions for the authenticated user",
)
async def logout_all(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> JSONResponse:
    """Revoke all sessions for the currently authenticated user."""
    count = await _service.logout_all(session=db, user_id=current_user.id)
    response = JSONResponse(content={"revoked_count": count}, status_code=status.HTTP_200_OK)
    _clear_auth_cookies(response)
    return response


# ---------------------------------------------------------------------------
# GET /auth/sessions
# ---------------------------------------------------------------------------


@router.get(
    "/sessions",
    response_model=list[SessionOut],
    summary="List active sessions for the authenticated user",
)
async def list_sessions(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[SessionOut]:
    """Return active (non-expired) refresh tokens for the current user."""
    sessions = await _service.list_sessions(session=db, user_id=current_user.id)
    return [SessionOut.model_validate(s) for s in sessions]


# ---------------------------------------------------------------------------
# DELETE /auth/sessions/{session_id}
# ---------------------------------------------------------------------------


@router.delete(
    "/sessions/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke a specific session by ID",
)
async def revoke_session(
    session_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> Response:
    """Revoke a single session owned by the current user."""
    await _service.revoke_session(session=db, user_id=current_user.id, token_id=session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
