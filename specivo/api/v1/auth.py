"""Auth endpoints: login, refresh, logout, session management.

Endpoints:
    POST   /auth/login           - Authenticate, return tokens + set cookies
    POST   /auth/refresh         - Rotate refresh token
    POST   /auth/logout          - Revoke one session (cookie-aware)
    POST   /auth/logout-all      - Revoke all sessions for the current user
    GET    /auth/sessions        - List active sessions
    DELETE /auth/sessions/{id}   - Revoke a specific session
    POST   /auth/change-password - Change your own password (authenticated)
    GET    /auth/me/email        - Your address and its pending change
    POST   /auth/me/email        - Ask to change your address (a link goes to the new one)
    DELETE /auth/me/email        - Cancel the pending change
    POST   /auth/me/email/resend - Send the link again (the previous one stops working)
    POST   /auth/confirm-email   - Use the link's token (no sign-in needed)
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Request, Response, status
from fastapi.responses import JSONResponse

from specivo.core.auth_cookies import ACCESS_COOKIE, REFRESH_COOKIE, clear_auth_cookies, set_auth_cookies
from specivo.core.client_ip import get_client_ip
from specivo.core.constants import API_KEY_PREFIX
from specivo.core.database import DbSession
from specivo.core.exceptions import AppError
from specivo.core.rate_limit import rate_limit
from specivo.core.security import get_account_user, request_session, request_session_id
from specivo.models.user import User
from specivo.schemas.auth import (
    ChangePasswordRequest,
    ChangePasswordResponse,
    EmailChangeAccepted,
    EmailChangeConfirmed,
    EmailChangeConfirmRequest,
    EmailChangeRequest,
    EmailChangeStateOut,
    ForgotPasswordRequest,
    LoginRequest,
    LogoutResponse,
    PendingEmailChangeOut,
    RefreshRequest,
    ResetPasswordRequest,
    SessionOut,
    TokenResponse,
)
from specivo.services.auth_service import AuthService
from specivo.services.auth_utils import validate_password_policy
from specivo.services.email_change_service import EmailChangeService

router = APIRouter()
_service = AuthService()
_email_change = EmailChangeService()


def _request_access_token(request: Request, cookie_value: str | None) -> str | None:
    """Return the access JWT the request carries: the Bearer header, else the cookie.

    An API key in the header is not an access token and yields the cookie.
    """
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        bearer = auth_header[len("Bearer ") :]
        if bearer and not bearer.startswith(API_KEY_PREFIX):
            return bearer
    return cookie_value


def _read_remember_from_access_cookie(request: Request, settings) -> bool:
    """Read the ``rem`` claim from the access token cookie.

    The token may be expired (that is the normal case during refresh), so
    we decode without verifying expiration.  Returns ``True`` for backward
    compatibility with tokens that predate the ``rem`` claim.
    """
    import jwt as pyjwt

    token = request.cookies.get(ACCESS_COOKIE)
    if not token:
        return True  # backward compat: no cookie → persistent (old behaviour)
    try:
        payload = pyjwt.decode(
            token,
            settings.secret_key,
            algorithms=["HS256"],
            options={"verify_exp": False},
        )
        return payload.get("rem", True)
    except Exception:
        return True  # decode failure → default to persistent


# ---------------------------------------------------------------------------
# POST /auth/login
# ---------------------------------------------------------------------------


_login_rate_limit = rate_limit("auth_login", max_requests=10, window_seconds=60)
_refresh_rate_limit = rate_limit("auth_refresh", max_requests=30, window_seconds=60)


@router.post(
    "/login/",
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
    db: DbSession,
    _rl: Annotated[None, Depends(_login_rate_limit)],
) -> Response:
    from specivo.core.config import get_settings

    settings = get_settings()
    device_info = request.headers.get("User-Agent")
    ip = get_client_ip(request)

    try:
        result = await _service.login(
            session=db,
            login_or_email=body.login,
            password=body.password,
            device_info=device_info,
            ip=ip,
            remember=body.remember,
        )
    except AppError as exc:
        # Log failed login attempt — commit before re-raising so the
        # audit row survives the get_db rollback on error.
        try:
            from specivo.services.security_audit_service import SecurityAuditService

            audit = SecurityAuditService()
            # Only the login of an account the attempt named, never the typed text:
            # a password typed into the login field must not reach the audit log.
            named = await _service.find_account(db, body.login)
            await audit.log_login_failure(
                session=db,
                request=request,
                login_hint=named.login if named is not None else None,
                reason=exc.code,
            )
            await db.commit()
        except Exception:
            pass  # Non-critical — never block error response
        raise

    access_token, refresh_token = result.access_token, result.refresh_token

    # Log successful login, and the hash upgrade it carried out if any.
    try:
        from specivo.services.security_audit_service import SecurityAuditService

        audit = SecurityAuditService()
        await audit.log_login_success(session=db, user_id=result.user_id, request=request)
        if result.hash_upgraded_from is not None:
            await audit.log_password_hash_upgraded(
                session=db,
                user_id=result.user_id,
                request=request,
                from_algorithm=result.hash_upgraded_from,
            )
    except Exception:
        pass  # Non-critical — never block login

    token_data = TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=settings.access_token_expire_minutes * 60,
    )
    response = JSONResponse(content=token_data.model_dump(), status_code=status.HTTP_200_OK)
    set_auth_cookies(response, access_token, refresh_token, settings, remember=body.remember)
    return response


# ---------------------------------------------------------------------------
# POST /auth/refresh
# ---------------------------------------------------------------------------


@router.post(
    "/refresh/",
    response_model=TokenResponse,
    summary="Rotate refresh token and issue new access token",
    responses={401: {"description": "Refresh token expired or revoked"}},
)
async def refresh(
    request: Request,
    db: DbSession,
    _rl: Annotated[None, Depends(_refresh_rate_limit)],
    body: RefreshRequest | None = None,
    refresh_token_cookie: Annotated[str | None, Cookie(alias=REFRESH_COOKIE)] = None,
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

    # Read the "remember me" preference from the existing access token JWT.
    # The token may be expired, so we decode without verifying expiration.
    remember = _read_remember_from_access_cookie(request, settings)

    access_token, new_refresh_token, _refreshed_user = await _service.refresh(
        session=db, refresh_token_raw=raw_token, remember=remember
    )

    token_data = TokenResponse(
        access_token=access_token,
        refresh_token=new_refresh_token,
        expires_in=settings.access_token_expire_minutes * 60,
    )
    response = JSONResponse(content=token_data.model_dump(), status_code=status.HTTP_200_OK)
    set_auth_cookies(response, access_token, new_refresh_token, settings, remember=remember)
    return response


# ---------------------------------------------------------------------------
# POST /auth/logout
# ---------------------------------------------------------------------------


@router.post(
    "/logout/",
    response_model=LogoutResponse,
    summary="End the current session",
)
async def logout(
    request: Request,
    db: DbSession,
    body: RefreshRequest | None = None,
    refresh_token_cookie: Annotated[str | None, Cookie(alias=REFRESH_COOKIE)] = None,
    access_token_cookie: Annotated[str | None, Cookie(alias=ACCESS_COOKIE)] = None,
) -> JSONResponse:
    """Revoke the session named by the refresh token and/or the access token.

    The refresh token comes from the body or the cookie, the access token from
    the ``Authorization: Bearer`` header or the cookie. The session they belong
    to is revoked: its refresh token can no longer rotate and its access
    tokens are refused from the next request on. Without either, this only
    clears the cookies.

    For a single sign-on session whose provider does RP-initiated logout,
    ``end_session_url`` is the identity provider's end-session URL; the client
    sends the user there to end the IdP session too. It is ``null`` otherwise.
    """
    from specivo.services.sso.logout import end_session_url, find_sso_logout_session

    raw_token = (body.refresh_token if body else None) or refresh_token_cookie
    access_raw = _request_access_token(request, access_token_cookie)
    sso_session = await find_sso_logout_session(db, refresh_token_raw=raw_token, access_token_raw=access_raw)
    await _service.end_browser_session(db, refresh_token_raw=raw_token, access_token_raw=access_raw)
    # The session is ended before the IdP is asked anything.
    await db.commit()

    url = await end_session_url(sso_session) if sso_session is not None else None
    response = JSONResponse(content=LogoutResponse(end_session_url=url).model_dump(), status_code=status.HTTP_200_OK)
    clear_auth_cookies(response)
    return response


# ---------------------------------------------------------------------------
# POST /auth/logout-all
# ---------------------------------------------------------------------------


@router.post(
    "/logout-all/",
    summary="Revoke all sessions for the authenticated user",
)
async def logout_all(
    db: DbSession,
    current_user: Annotated[User, Depends(get_account_user)],
) -> JSONResponse:
    """Revoke all sessions for the currently authenticated user."""
    count = await _service.logout_all(session=db, user_id=current_user.id)
    response = JSONResponse(content={"revoked_count": count}, status_code=status.HTTP_200_OK)
    clear_auth_cookies(response)
    return response


# ---------------------------------------------------------------------------
# GET /auth/sessions
# ---------------------------------------------------------------------------


@router.get(
    "/sessions/",
    response_model=list[SessionOut],
    summary="List active sessions for the authenticated user",
)
async def list_sessions(
    db: DbSession,
    current_user: Annotated[User, Depends(get_account_user)],
) -> list[SessionOut]:
    """Return active (non-expired) refresh tokens for the current user."""
    sessions = await _service.list_sessions(session=db, user_id=current_user.id)
    return [
        SessionOut.model_validate(token).model_copy(update={"provider_name": provider_name})
        for token, provider_name in sessions
    ]


# ---------------------------------------------------------------------------
# DELETE /auth/sessions/{session_id}
# ---------------------------------------------------------------------------


@router.delete(
    "/sessions/{session_id}/",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke a specific session by ID",
)
async def revoke_session(
    session_id: int,
    db: DbSession,
    current_user: Annotated[User, Depends(get_account_user)],
) -> Response:
    """Revoke a single session owned by the current user."""
    await _service.revoke_session(session=db, user_id=current_user.id, token_id=session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# POST /auth/forgot-password
# ---------------------------------------------------------------------------

_forgot_password_rate_limit = rate_limit("auth_forgot_password", max_requests=5, window_seconds=300)


@router.post(
    "/forgot-password/",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Request a password reset email",
    responses={429: {"description": "Rate limit exceeded"}},
)
async def forgot_password(
    body: ForgotPasswordRequest,
    request: Request,
    db: DbSession,
    _rl: Annotated[None, Depends(_forgot_password_rate_limit)],
) -> dict:
    """Send a password reset email if the address is associated with an account.

    Always returns 202 regardless of whether the email exists — no enumeration.
    """
    user_id = await _service.request_password_reset(session=db, email=body.email)

    # Audit: log the request (user_id is None if email didn't match)
    try:
        from specivo.services.security_audit_service import SecurityAuditService

        audit = SecurityAuditService()
        await audit.log_password_reset_requested(
            session=db,
            user_id=user_id,
            request=request,
            email_hint=body.email,
        )
    except Exception:
        pass  # Non-critical — never block the response

    return {"detail": "If the email is registered, a reset link has been sent."}


# ---------------------------------------------------------------------------
# POST /auth/reset-password
# ---------------------------------------------------------------------------


@router.post(
    "/reset-password/",
    status_code=status.HTTP_200_OK,
    summary="Reset password using a token from the reset email",
    responses={400: {"description": "Invalid, expired, or already-used token"}},
)
async def reset_password(
    body: ResetPasswordRequest,
    request: Request,
    db: DbSession,
) -> dict:
    """Set a new password using a valid reset token."""
    validate_password_policy(body.new_password)

    try:
        user_id = await _service.reset_password_with_token(
            session=db,
            token=body.token,
            new_password=body.new_password,
        )
    except AppError as exc:
        # Audit: log the failure before re-raising
        try:
            from specivo.services.security_audit_service import SecurityAuditService

            audit = SecurityAuditService()
            await audit.log_password_reset_failed(
                session=db,
                reason=exc.code,
                request=request,
            )
            await db.commit()
        except Exception:
            pass  # Non-critical — never block error response
        raise

    # Audit: log the successful reset
    try:
        from specivo.services.security_audit_service import SecurityAuditService

        audit = SecurityAuditService()
        await audit.log_password_reset_completed(
            session=db,
            user_id=user_id,
            request=request,
        )
    except Exception:
        pass  # Non-critical — never block the response

    return {"detail": "Password has been reset successfully."}


# ---------------------------------------------------------------------------
# POST /auth/change-password
# ---------------------------------------------------------------------------

# A stolen session must not become a brute-force oracle for the current
# password, so this is limited like /auth/forgot-password (5 per 5 minutes)
# rather than like /auth/login (10 per minute): a legitimate user changes
# their password once, while an attacker gets roughly one bcrypt guess a
# minute. The identifier is the client IP, as on every other auth route.
_change_password_rate_limit = rate_limit("auth_change_password", max_requests=5, window_seconds=300)


@router.post(
    "/change-password/",
    response_model=ChangePasswordResponse,
    summary="Change the authenticated user's own password",
    responses={
        400: {"description": "Current password incorrect, unchanged, or account has no password"},
        401: {"description": "Not authenticated"},
        422: {"description": "New password fails the password policy"},
        429: {"description": "Rate limit exceeded"},
    },
)
async def change_password(
    body: ChangePasswordRequest,
    request: Request,
    db: DbSession,
    current_user: Annotated[User, Depends(get_account_user)],
    _rl: Annotated[None, Depends(_change_password_rate_limit)],
    refresh_token_cookie: Annotated[str | None, Cookie(alias=REFRESH_COOKIE)] = None,
    access_token_cookie: Annotated[str | None, Cookie(alias=ACCESS_COOKIE)] = None,
) -> ChangePasswordResponse:
    """Change your own password, proving ownership with the current one.

    On success every *other* session of this user is revoked; the session
    making the request survives, so the caller is not signed out of the page
    they changed their password on.
    """
    from specivo.services.security_audit_service import SecurityAuditService

    audit = SecurityAuditService()

    try:
        hash_upgraded_from = await _service.change_password(
            session=db,
            user=current_user,
            current_password=body.current_password,
            new_password=body.new_password,
        )
    except AppError as exc:
        # Audit the rejection, committing before re-raising so the row
        # survives the get_db rollback on error.
        try:
            await audit.log_password_change_failed(
                session=db,
                reason=exc.code,
                user_id=current_user.id,
                request=request,
            )
            await db.commit()
        except Exception:
            pass  # Non-critical — never block the error response
        raise

    # Revoke the user's other sessions, keeping the caller's own refresh
    # token alive so a browser session survives its own password change. A
    # Bearer caller without one loses its refresh token too, but its access
    # token is not cut off mid-flow.
    revoked = await _service.logout_other_sessions(
        session=db,
        user_id=current_user.id,
        keep_refresh_token_raw=refresh_token_cookie,
        caller_access_token_raw=_request_access_token(request, access_token_cookie),
    )

    try:
        if hash_upgraded_from is not None:
            await audit.log_password_hash_upgraded(
                session=db,
                user_id=current_user.id,
                request=request,
                from_algorithm=hash_upgraded_from,
            )
        await audit.log_password_changed(
            session=db,
            user_id=current_user.id,
            request=request,
            revoked_sessions=revoked,
        )
    except Exception:
        pass  # Non-critical — never block the response

    # Commit before responding: the browser reloads its session state as soon
    # as it sees the success payload (ADR-0004 §12).
    await db.commit()

    return ChangePasswordResponse(
        detail="Password changed successfully.",
        revoked_sessions=revoked,
    )


# ---------------------------------------------------------------------------
# Email change: /auth/me/email/ and /auth/confirm-email/
# ---------------------------------------------------------------------------

#: Same wording whether or not another account uses the address (no enumeration).
EMAIL_CHANGE_ACCEPTED_DETAIL = (
    "Check the inbox of the new address and open the link to confirm the change. "
    "Your current address stays in use until then."
)
_confirm_email_rate_limit = rate_limit("auth_confirm_email", max_requests=20, window_seconds=60)


def _pending_out(pending) -> PendingEmailChangeOut:
    return PendingEmailChangeOut(
        new_email=pending.new_email,
        requested_at=pending.requested_at,
        expires_at=pending.expires_at,
        expired=pending.expired,
    )


@router.get(
    "/me/email/",
    response_model=EmailChangeStateOut,
    summary="Your email address and its pending change",
)
async def get_email_change(
    request: Request,
    db: DbSession,
    current_user: Annotated[User, Depends(get_account_user)],
) -> EmailChangeStateOut:
    """The current address, the pending change (``null`` without one) and whether a change is possible."""
    session_id = await request_session_id(request, db, current_user.id)
    state = await _email_change.state(db, current_user, session_id=session_id)
    return EmailChangeStateOut(
        email=state.email,
        pending=_pending_out(state.pending) if state.pending is not None else None,
        managed_by_idp=state.managed_by_idp,
        requires_reauth=state.requires_reauth,
        reauthenticated=state.reauthenticated,
        available=not (state.managed_by_idp or state.unavailable),
    )


@router.post(
    "/me/email/",
    response_model=EmailChangeAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ask to change your email address",
    responses={
        400: {"description": "Wrong current password, the current address, or the account cannot change it"},
        403: {"description": "Managed by the identity provider, or a fresh sign-in at the IdP is needed"},
        422: {"description": "Not a valid email address"},
        429: {"description": "Too many requests for this account or this address"},
    },
)
async def request_email_change(
    body: EmailChangeRequest,
    request: Request,
    db: DbSession,
    current_user: Annotated[User, Depends(get_account_user)],
) -> EmailChangeAccepted:
    """Send a confirmation link to ``new_email``; the address changes when the link is used.

    The answer is the same whether or not another account uses the address.
    A new request replaces the pending one: only the latest link works.
    """
    session_id = await request_session_id(request, db, current_user.id)
    pending = await _email_change.request_change(
        db,
        current_user,
        new_email=body.new_email,
        current_password=body.current_password,
        session_id=session_id,
    )
    await db.commit()
    return EmailChangeAccepted(detail=EMAIL_CHANGE_ACCEPTED_DETAIL, pending=_pending_out(pending))


@router.post(
    "/me/email/resend/",
    response_model=EmailChangeAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Send the confirmation link again",
    responses={404: {"description": "No pending change"}, 429: {"description": "Too many requests"}},
)
async def resend_email_change(
    db: DbSession,
    current_user: Annotated[User, Depends(get_account_user)],
) -> EmailChangeAccepted:
    """Send a new link for the pending change; the previous link stops working."""
    pending = await _email_change.resend(db, current_user)
    await db.commit()
    return EmailChangeAccepted(detail=EMAIL_CHANGE_ACCEPTED_DETAIL, pending=_pending_out(pending))


@router.delete(
    "/me/email/",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Cancel the pending email change",
)
async def cancel_email_change(
    db: DbSession,
    current_user: Annotated[User, Depends(get_account_user)],
) -> Response:
    """Cancel the pending change, if any; its link stops working."""
    await _email_change.cancel(db, current_user)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/confirm-email/",
    response_model=EmailChangeConfirmed,
    summary="Confirm an email change with the token from the link",
    responses={
        400: {"description": "Unknown, used, replaced, cancelled or expired link"},
        409: {"description": "Another account uses the address now"},
        429: {"description": "Rate limit exceeded"},
    },
)
async def confirm_email_change(
    body: EmailChangeConfirmRequest,
    request: Request,
    db: DbSession,
    _rl: Annotated[None, Depends(_confirm_email_rate_limit)],
) -> EmailChangeConfirmed:
    """Change the address. No sign-in is needed: the token is the proof.

    Every other session of the account ends; a caller signed in as the same
    user keeps the session it called with.
    """
    caller = await request_session(request, db)
    result = await _email_change.confirm(
        db,
        body.token,
        caller_user_id=caller[0] if caller else None,
        caller_session_id=caller[1] if caller else None,
    )
    await db.commit()
    return EmailChangeConfirmed(
        detail="Your email address was changed.", email=result.new_email, revoked_sessions=result.revoked_sessions
    )
