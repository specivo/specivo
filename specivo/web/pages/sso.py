"""SSO (OpenID Connect) browser routes: start a login or a link, receive the callback.

``GET /auth/oidc/{slug}/login/?next=`` redirects to the IdP and sets the
``spv_sso_tx`` binding cookie. ``GET /auth/oidc/{slug}/callback/`` finishes
the login, sets the usual auth cookies and redirects to ``next``. Both are
GET-only, so the CSRF middleware has nothing to exempt: the transaction's
state, bound to the browser by the cookie, is the CSRF defence of the flow.

``POST /auth/oidc/{slug}/link/`` (signed in, CSRF-protected by the
middleware, 5 per minute per user) starts the same flow with
``intent=link``. Its callback connects the identity to the signed-in user,
starts no session, and returns to ``/my/profile/?sso_linked=<slug>``.

A refused login redirects to ``/login/?sso_error=<code>``, a refused link
to ``/my/profile/?sso_error=<code>``; both pages turn the code into a
translated message (:func:`sso_error_message`) and never show anything the
IdP sent. Unknown or disabled providers are 404. The protocol work lives in
:class:`specivo.services.sso.login.SsoLoginService`.

``POST /auth/oidc/{slug}/backchannel-logout/`` is called by the IdP, server
to server, with a form-encoded ``logout_token`` (OpenID Connect Back-Channel
Logout 1.0). No cookies, no session: the signed, single-use token is the
proof, so the CSRF middleware exempts this one path by an anchored pattern.
Answers, always with ``Cache-Control: no-store``: 200 (empty) when the token
was valid, whether or not a session matched; 400 ``{"error":
"invalid_request"}`` for a missing, malformed, invalid or replayed token;
404 when the provider is unknown, disabled or has back-channel logout off;
503 ``{"error": "temporarily_unavailable"}`` when the replay cache or the
IdP's keys cannot be reached, so the IdP may retry. The work lives in
:func:`specivo.services.sso.logout.backchannel_logout`.
"""

from __future__ import annotations

from typing import Annotated, Any
from urllib.parse import parse_qs, quote, urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.auth_cookies import set_auth_cookies
from specivo.core.client_ip import get_client_ip
from specivo.core.config import Settings, get_settings
from specivo.core.database import get_db
from specivo.core.i18n import gettext as _
from specivo.core.rate_limit import enforce_rate_limit, rate_limit
from specivo.core.utils import utcnow
from specivo.models.sso import SsoProvider
from specivo.models.user import User
from specivo.services.security_audit_service import SecurityAuditService
from specivo.services.sso.errors import SsoError, SsoTokenInvalidError
from specivo.services.sso.flow import TX_COOKIE_NAME, TX_TTL_SECONDS, tx_cookie_path
from specivo.services.sso.log import logger
from specivo.services.sso.login import LINK_RETURN_PATH, SsoLinkResult, SsoLoginService
from specivo.services.sso.logout import backchannel_logout, release_logout_jti
from specivo.web.deps import get_current_user_optional, require_user

router = APIRouter(prefix="/auth/oidc", tags=["web-sso"], include_in_schema=False)
_service = SsoLoginService()

_start_rate_limit = rate_limit("sso_login_start", max_requests=20, window_seconds=60)
_callback_rate_limit = rate_limit("sso_callback", max_requests=20, window_seconds=60)
# Back-channel logout is metered per provider, not per sender: the IdP is one
# sender for all its users, and a busy or spoofing sender must not use up
# another provider's budget. Checked once the provider is known to exist, so
# requests for made-up slugs create no buckets.
_BACKCHANNEL_RATE_LIMIT = ("sso_backchannel_logout", 60, 60)

#: Largest back-channel logout request body read; a logout token is a few KB.
BACKCHANNEL_MAX_BODY_BYTES = 16 * 1024
_FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"
_LINK_RATE_LIMIT = ("sso_link_start", 5, 60)

# Callback parameters that must appear at most once (a repeated one is refused).
_SINGLE_VALUED = ("state", "code", "error", "iss")

# Error codes shown to the browser under the name of the design's error table.
# The token endpoint refusing the code (invalid_grant, ...) is, for the user,
# the identity provider refusing the sign-in.
_PUBLIC_CODES = {"sso_token_exchange_failed": "sso_idp_error"}


def sso_error_message(code: str | None) -> str | None:
    """The translated login-page message for an ``sso_error`` code.

    ``None`` when there is no code. Unknown codes get the generic message, so
    the parameter is never reflected into the page.
    """
    if not code:
        return None
    messages = {
        "sso_state_invalid": _("Your sign-in expired or was started in another browser. Please try again."),
        "sso_idp_error": _("The identity provider refused the sign-in."),
        "sso_idp_unreachable": _("The identity provider is unavailable. Please try again later."),
        "sso_discovery_invalid": _("Single sign-on is not configured correctly. Contact your administrator."),
        "sso_provider_misconfigured": _("Single sign-on is not configured correctly. Contact your administrator."),
        "sso_account_not_provisioned": _(
            "No account here is connected to your identity provider account. Contact your administrator."
        ),
        "sso_account_disabled": _("This account is disabled. Contact your administrator."),
        "sso_unavailable": _("Single sign-on is temporarily unavailable. Please try again later."),
        "sso_email_missing": _("The identity provider did not send an email address. Contact your administrator."),
        "sso_email_conflict": _(
            "An account with this email address already exists. "
            "Sign in with your password and connect the account from your profile."
        ),
        "sso_identity_in_use": _("This identity provider account is already connected to another account."),
        "sso_provider_already_linked": _("An account at this identity provider is already connected to your profile."),
        "sso_domain_not_allowed": _("Your email domain is not allowed to sign in here. Contact your administrator."),
        "sso_not_in_required_group": _(
            "Your identity provider account is not in a group that may sign in here. Contact your administrator."
        ),
        "sso_groups_overage": _(
            "Your identity provider sent too many groups to read. Ask your administrator to send only the "
            "groups assigned to the application, or app roles."
        ),
        "sso_last_sign_in_method": _(
            "This is your only way to sign in. Set a password or connect another account before disconnecting it."
        ),
    }
    return messages.get(code) or _("Single sign-on failed. Please try again.")


def _prefix(settings: Settings) -> str:
    return settings.stealth_prefix.rstrip("/")


def sso_login_path(slug: str, next_path: str = "") -> str:
    """The URL that starts a login at provider ``slug`` (stealth prefix included).

    ``next_path`` must already have been through ``safe_next_path``; an empty
    one is left out.
    """
    path = f"{_prefix(get_settings())}/auth/oidc/{quote(slug, safe='')}/login/"
    return f"{path}?{urlencode({'next': next_path})}" if next_path else path


def _clear_tx_cookie(response: RedirectResponse, settings: Settings) -> None:
    response.delete_cookie(
        TX_COOKIE_NAME,
        path=tx_cookie_path(settings),
        secure=not settings.debug,
        httponly=True,
        samesite="lax",
    )


def _refuse(code: str, settings: Settings, *, intent: str = "login", status_code: int = 302) -> RedirectResponse:
    """Redirect a refusal to the login page, or to the profile for a link."""
    query = urlencode({"sso_error": _PUBLIC_CODES.get(code, code)})
    page = LINK_RETURN_PATH if intent == "link" else "/login/"
    response = RedirectResponse(f"{_prefix(settings)}{page}?{query}", status_code=status_code)
    _clear_tx_cookie(response, settings)
    return response


def _set_tx_cookie(response: RedirectResponse, binding_token: str, settings: Settings) -> None:
    response.set_cookie(
        TX_COOKIE_NAME,
        binding_token,
        max_age=TX_TTL_SECONDS,
        path=tx_cookie_path(settings),
        secure=not settings.debug,
        httponly=True,
        samesite="lax",
    )


async def _provider_or_404(db: AsyncSession, slug: str) -> SsoProvider:
    provider = await _service.get_enabled_provider(db, slug)
    if provider is None:
        raise HTTPException(status_code=404, detail="Not found")
    return provider


async def _audit_failure(db: AsyncSession, request: Request, provider: SsoProvider, exc: SsoError) -> None:
    logger.warning("sso.login.failed provider=%s code=%s reason=%s", provider.slug, exc.code, exc.reason)
    try:
        await SecurityAuditService().log_sso_login_failure(
            db, provider_slug=provider.slug, issuer=provider.issuer, reason=exc.code, request=request
        )
    except Exception:
        logger.exception("sso.audit.failed provider=%s", provider.slug)


@router.get("/{slug}/login/", response_model=None)
async def sso_login_start(
    slug: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    _rl: Annotated[None, Depends(_start_rate_limit)],
    next_url: Annotated[str, Query(alias="next")] = "",
) -> RedirectResponse:
    """Send the browser to the IdP's authorization endpoint (302)."""
    settings = get_settings()
    provider = await _provider_or_404(db, slug)
    try:
        authorization = await _service.start(provider, next_path=next_url)
    except SsoError as exc:
        logger.warning("sso.login.start_failed provider=%s code=%s reason=%s", provider.slug, exc.code, exc.reason)
        return _refuse(exc.code, settings)

    response = RedirectResponse(authorization.url, status_code=302)
    _set_tx_cookie(response, authorization.binding_token, settings)
    response.headers["Cache-Control"] = "no-store"
    return response


async def _link_user(
    request: Request,
    response: Response,
    user: Annotated[User, Depends(require_user)],
) -> User:
    """The signed-in user, after the per-user link rate limit."""
    request.state.rate_limit_user_id = user.id
    await enforce_rate_limit(request, response, *_LINK_RATE_LIMIT)
    return user


@router.post("/{slug}/link/", response_model=None)
async def sso_link_start(
    slug: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(_link_user)],
) -> RedirectResponse:
    """Connect an identity at this provider to the signed-in user: 303 to the IdP."""
    settings = get_settings()
    provider = await _provider_or_404(db, slug)
    try:
        authorization = await _service.start_link(db, provider, user_id=user.id)
    except SsoError as exc:
        logger.warning(
            "sso.link.start_failed provider=%s user_id=%s code=%s reason=%s",
            provider.slug,
            user.id,
            exc.code,
            exc.reason,
        )
        return _refuse(exc.code, settings, intent="link", status_code=303)

    response = RedirectResponse(authorization.url, status_code=303)
    _set_tx_cookie(response, authorization.binding_token, settings)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/{slug}/callback/", response_model=None)
async def sso_callback(
    slug: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    _rl: Annotated[None, Depends(_callback_rate_limit)],
) -> RedirectResponse:
    """Finish the login: new session cookies and a redirect to ``next``, or a refusal redirect."""
    settings = get_settings()
    provider = await _provider_or_404(db, slug)

    query = request.query_params
    if any(len(query.getlist(name)) > 1 for name in _SINGLE_VALUED):
        logger.warning("sso.callback.rejected provider=%s reason=repeated_parameter", provider.slug)
        return _refuse("sso_state_invalid", settings)

    async def current_user_id() -> int | None:
        try:
            user = await get_current_user_optional(request, db)
        except HTTPException:
            return None
        return user.id if isinstance(user, User) else None

    try:
        result = await _service.finish(
            db,
            provider,
            params={key: value for key, value in query.items()},
            binding_token=request.cookies.get(TX_COOKIE_NAME),
            device_info=request.headers.get("User-Agent"),
            ip=get_client_ip(request),
            current_user_id=current_user_id,
        )
    except SsoError as exc:
        if exc.intent == "link":
            logger.warning("sso.link.failed provider=%s code=%s reason=%s", provider.slug, exc.code, exc.reason)
            return _refuse(exc.code, settings, intent="link")
        await _audit_failure(db, request, provider, exc)
        return _refuse(exc.code, settings)

    if isinstance(result, SsoLinkResult):
        await db.commit()
        target = f"{_prefix(settings)}{LINK_RETURN_PATH}?{urlencode({'sso_linked': result.provider_slug})}"
        response = RedirectResponse(target, status_code=302)
        _clear_tx_cookie(response, settings)
        response.headers["Cache-Control"] = "no-store"
        return response

    try:
        await SecurityAuditService().log_sso_login_success(
            db,
            user_id=result.user_id,
            provider_slug=provider.slug,
            issuer=provider.issuer,
            sub_hash=result.sub_hash,
            request=request,
        )
    except Exception:
        logger.exception("sso.audit.failed provider=%s", provider.slug)

    # Commit before the browser follows the redirect, so the new session
    # exists on the very next request.
    await db.commit()

    response = RedirectResponse(result.next_path or f"{_prefix(settings)}/", status_code=302)
    remaining = int((result.absolute_expires_at - utcnow()).total_seconds())
    set_auth_cookies(
        response,
        result.access_token,
        result.refresh_token,
        settings,
        remember=True,
        refresh_max_age=remaining,
    )
    _clear_tx_cookie(response, settings)
    response.headers["Cache-Control"] = "no-store"
    return response


# ---------------------------------------------------------------------------
# Back-channel logout
# ---------------------------------------------------------------------------


class _BadLogoutRequestError(Exception):
    """The request carries no usable ``logout_token``; ``reason`` is for the log."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _backchannel_answer(status: int, body: dict[str, Any] | None = None) -> Response:
    response: Response = JSONResponse(body, status_code=status) if body is not None else Response(status_code=status)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response


async def _read_logout_token(request: Request) -> str:
    """The single ``logout_token`` of a form-encoded body of at most 16 KB."""
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type != _FORM_CONTENT_TYPE:
        raise _BadLogoutRequestError("content_type")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > BACKCHANNEL_MAX_BODY_BYTES:
            raise _BadLogoutRequestError("body_too_large")
    try:
        fields = parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=16)
    except (UnicodeDecodeError, ValueError):
        raise _BadLogoutRequestError("malformed_body") from None
    values = fields.get("logout_token", [])
    if len(values) != 1:
        raise _BadLogoutRequestError("missing_token" if not values else "repeated_token")
    token = values[0].strip()
    if not token:
        raise _BadLogoutRequestError("missing_token")
    return token


@router.post("/{slug}/backchannel-logout/", response_model=None)
async def sso_backchannel_logout(
    slug: str,
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """End the Specivo sessions a valid logout token names (see the module docstring)."""
    provider = await _service.get_enabled_provider(db, slug)
    if provider is None or not provider.backchannel_logout_enabled:
        return _backchannel_answer(404, {"error": "not_found"})
    await enforce_rate_limit(request, response, *_BACKCHANNEL_RATE_LIMIT, identifier=f"provider:{provider.id}")

    try:
        logout_token = await _read_logout_token(request)
    except _BadLogoutRequestError as exc:
        logger.warning("sso.backchannel_logout.rejected provider=%s reason=%s", provider.slug, exc.reason)
        return _backchannel_answer(400, {"error": "invalid_request"})

    try:
        result = await backchannel_logout(db, provider, logout_token)
    except SsoTokenInvalidError:
        # verify_logout_token and the replay check have logged the reason.
        return _backchannel_answer(400, {"error": "invalid_request"})
    except SsoError as exc:
        logger.warning(
            "sso.backchannel_logout.failed provider=%s code=%s reason=%s", provider.slug, exc.code, exc.reason
        )
        return _backchannel_answer(503, {"error": "temporarily_unavailable"})

    # 200 tells the IdP the sessions are gone: commit before answering.
    try:
        await db.commit()
    except BaseException:
        await release_logout_jti(result.jti_key)
        raise
    return _backchannel_answer(200)
