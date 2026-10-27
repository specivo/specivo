"""Web auth pages: login, logout, password reset, API keys management."""

from __future__ import annotations

import hashlib
import logging
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import get_db
from specivo.core.utils import utcnow
from specivo.models.user import User
from specivo.web.deps import (
    get_current_user_optional,
    get_templates,
    require_user,
    safe_next_path,
)
from specivo.web.pages.sso import sso_error_message, sso_login_path

logger = logging.getLogger(__name__)

router = APIRouter(tags=["web-auth"], include_in_schema=False)


@router.get("/login/", response_model=None)
async def login_page(
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
    reset: str = Query(""),  # noqa: B008
    next_url: str = Query("", alias="next"),  # noqa: B008
    sso_error: str = Query(""),  # noqa: B008
    logged_out: str = Query(""),  # noqa: B008
    local: str = Query(""),  # noqa: B008
) -> HTMLResponse | RedirectResponse:
    """Render the login page (standalone layout, no sidebar).

    If the user is already authenticated, the template shows a
    'You are logged in' widget instead of the login form.
    Accepts ``?reset=ok`` query param to show a success banner after password reset.

    ``?next=`` is where the visitor was heading: the pages that refuse a
    visitor without an account send it along so signing in returns them to the
    page they asked for instead of the dashboard. It is validated here rather
    than trusted — only a path within this site survives ``safe_next_path`` —
    so the parameter cannot be used to bounce somebody to another origin.

    ``?sso_error=`` is where a refused single sign-on lands. Only its code is
    read, and only a fixed, translated message is shown for it
    (``sso_error_message``); nothing the identity provider sent reaches the
    page.

    ``?logged_out=1`` is where sign-out lands (directly, or back from the
    identity provider) and shows a "You have been signed out" notice.

    Single sign-on (``get_login_policy``, which applies ``SSO_FORCE_LOCAL_LOGIN``):

    * every enabled provider gets a "Sign in with ..." link carrying ``next``;
    * ``sso.hide_local_login`` hides the password form behind a link to
      ``?local=1``, the break-glass way back to it. With no enabled provider
      the form is shown anyway, since there would be nothing else;
    * ``sso.auto_redirect_provider`` answers with a redirect to that provider,
      except for a signed-in visitor, ``?local=1``, ``?logged_out=1``,
      ``?reset=ok``, any ``sso_error`` (a failed SSO login must not loop) and a
      provider that is disabled or gone.
    """
    from specivo.services.sso.auth_settings import get_login_policy
    from specivo.services.sso.login import SsoLoginService

    user = await get_current_user_optional(request, db)
    next_path = safe_next_path(next_url)
    want_local = local == "1"
    policy = await get_login_policy(db)
    providers = await SsoLoginService().list_enabled_providers(db)

    redirect_slug = policy.auto_redirect_provider
    if (
        redirect_slug
        and user is None
        and not want_local
        and logged_out != "1"
        and reset != "ok"
        and not sso_error
        and any(p.slug == redirect_slug for p in providers)
    ):
        response = RedirectResponse(sso_login_path(redirect_slug, next_path), status_code=302)
        response.headers["Cache-Control"] = "no-store"
        return response

    show_local_form = want_local or not policy.hide_local_login or not providers
    local_query = {"local": "1", **({"next": next_path} if next_path else {})}
    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/auth/login.html",
        context={
            "user": user,
            "reset_ok": reset == "ok",
            "next_path": next_path,
            "sso_error_message": sso_error_message(sso_error),
            "sso_idp_unreachable": sso_error == "sso_idp_unreachable",
            "logged_out": logged_out == "1",
            "sso_buttons": [
                {"display_name": p.display_name, "url": sso_login_path(p.slug, next_path)} for p in providers
            ],
            "show_local_form": show_local_form,
            "local_login_url": "?" + urlencode(local_query),
            "local_login_admins_only": policy.local_login_admins_only,
        },
    )


@router.get("/logout/", response_model=None)
async def logout_confirm_page(
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> HTMLResponse | RedirectResponse:
    """Ask before signing out; a GET never signs anybody out.

    Signing out is a state change, so it only happens on the CSRF-protected
    ``POST /logout/``. Otherwise any page could sign the visitor out with an
    ``<img src="/logout/">``. A bookmark or an old link lands here and gets a
    one-button form instead. A visitor with no session has nothing to end and
    goes straight to the login page.
    """
    user = await get_current_user_optional(request, db)
    if user is None:
        return RedirectResponse("/login/", status_code=302)
    templates = get_templates()
    return templates.TemplateResponse(request, "pages/auth/logout_confirm.html", context={"user": user})


@router.post("/logout/")
async def logout(
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> RedirectResponse:
    """Revoke this browser's session on the server, clear its cookies, sign out at the IdP.

    The session is revoked through ``SessionService.revoke`` (its refresh rows
    are deleted and its ``sid`` is marked revoked) and the access token's
    ``jti`` is blocklisted, so copies of either cookie stop working
    immediately. CSRF is enforced by ``CSRFMiddleware`` whenever the request
    carries a session cookie; without one there is nothing to revoke.

    An SSO session is read before it is revoked: when its provider does
    RP-initiated logout, the browser goes on to the IdP's end-session endpoint
    (``specivo.services.sso.logout``), which sends it back to the fixed
    ``/login/?logged_out=1``. Everything else lands there directly. Nothing in
    the request chooses the destination.
    """
    from specivo.services.auth_service import AuthService
    from specivo.services.sso.logout import LOGGED_OUT_PATH, end_session_url, find_sso_logout_session

    refresh_raw = request.cookies.get("refresh_token")
    access_raw = request.cookies.get("access_token")
    sso_session = await find_sso_logout_session(db, refresh_token_raw=refresh_raw, access_token_raw=access_raw)
    await AuthService().end_browser_session(db, refresh_token_raw=refresh_raw, access_token_raw=access_raw)
    # Commit before the IdP is asked anything and before the browser follows
    # the redirect, so the revoked session is gone whatever happens next.
    await db.commit()

    target = (await end_session_url(sso_session) if sso_session is not None else None) or LOGGED_OUT_PATH
    response = RedirectResponse(target, status_code=302)
    response.delete_cookie("access_token", path="/")
    response.delete_cookie("refresh_token", path="/")
    return response


@router.get("/forgot-password/", response_class=HTMLResponse)
async def forgot_password_form(request: Request) -> HTMLResponse:
    """Render the forgot password form."""
    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/auth/forgot_password.html",
    )


@router.get("/reset-password/", response_class=HTMLResponse)
async def reset_password_form(
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
    token: str = Query(""),  # noqa: B008
) -> HTMLResponse:
    """Render the reset password form.

    Validates the token server-side before showing the form.
    If the token is invalid or expired, shows an error message instead.
    """
    from specivo.core.i18n import gettext as _
    from specivo.models.auth import PasswordResetToken

    templates = get_templates()
    token_error = None

    if not token:
        token_error = _("No reset token provided. Please use the link from your email.")
    else:
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        stmt = select(PasswordResetToken).where(PasswordResetToken.token_hash == token_hash)
        result = await db.execute(stmt)
        record = result.scalar_one_or_none()

        if record is None:
            token_error = _("This password reset link is invalid. Please request a new one.")
        elif record.used_at is not None:
            token_error = _("This password reset link has already been used. Please request a new one.")
        elif record.expires_at < utcnow():
            token_error = _("This password reset link has expired. Please request a new one.")

    return templates.TemplateResponse(
        request,
        "pages/auth/reset_password.html",
        context={
            "token": token,
            "token_error": token_error,
        },
    )


@router.get("/my/profile/", response_model=None)
async def profile_page(
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: AsyncSession = Depends(get_db),  # noqa: B008
    sso_linked: str = Query(""),  # noqa: B008
    sso_unlinked: str = Query(""),  # noqa: B008
    sso_error: str = Query(""),  # noqa: B008
) -> Response:
    """Render the user profile page, with its "Connected accounts" section.

    ``?sso_linked=``, ``?sso_unlinked=`` and ``?sso_error=`` are where the
    connect and disconnect flows land; each selects a fixed, translated
    message and is never reflected.
    """
    from specivo.web.pages.connected_accounts import profile_connected_accounts

    connected = await profile_connected_accounts(db, user, linked=sso_linked, unlinked=sso_unlinked, error=sso_error)
    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/auth/profile.html",
        context={"user": user, "active_page": "profile", **connected},
    )


@router.post("/my/profile/", response_model=None)
async def update_profile(
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: AsyncSession = Depends(get_db),  # noqa: B008
    display_name: str = Form(""),
) -> Response:
    """Update user profile (display name)."""
    display_name = display_name.strip()
    if display_name and display_name != user.display_name:
        user.display_name = display_name
        await db.commit()

    return RedirectResponse("/my/profile/", status_code=303)


@router.get("/my/preferences/", response_model=None)
async def preferences_page(
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render the user preferences page."""
    from specivo.core.config import get_settings
    from specivo.core.locales import TIMEZONE_CHOICES, get_language_choices
    from specivo.services.auth_service import AuthService
    from specivo.services.settings_service import SettingsService

    settings = get_settings()
    palette = await SettingsService().get_avatar_palette(db)
    sessions = await AuthService().list_sessions(db, user.id)
    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/auth/preferences.html",
        context={
            "user": user,
            "active_page": "preferences",
            "avatar_palette": palette,
            "sessions": [{"row": row, "provider_name": provider_name} for row, provider_name in sessions],
            "sso_password_providers": await _sso_only_providers(db, user),
            "language_choices": get_language_choices(settings.available_languages),
            "timezone_choices": TIMEZONE_CHOICES,
            "password_min_length": settings.password_min_length,
        },
    )


async def _sso_only_providers(db: AsyncSession, user: User) -> list[str]:
    """Display names of the providers ``user`` signs in with, when that is the only way in.

    Empty for a user with a password, a service account, or a user without a
    connected identity: they get the ordinary password form.
    """
    if user.password_hash or user.is_service_account:
        return []
    from specivo.services.sso.identity_service import IdentityService

    identities = await IdentityService().list_for_user(db, user.id)
    return sorted({identity.provider_name for identity in identities})


@router.get("/my/password/", response_model=None)
async def change_password_page(
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render the standalone password-change page.

    ``require_user`` sends a user carrying ``must_change_password`` here from
    every other page and lets this one through, so this is the end of that
    redirect rather than another hop in it. The same page serves a voluntary
    visit, which gets the form without the explanation and without being sent
    anywhere afterwards.

    A user without a password who signs in through an identity provider gets
    an explanation instead of the form: there is no current password to
    prove, and the change endpoint refuses them.
    """
    from specivo.core.config import get_settings

    settings = get_settings()
    forced = bool(user.must_change_password)
    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/auth/change_password.html",
        context={
            "user": user,
            "forced": forced,
            "sso_password_providers": await _sso_only_providers(db, user),
            "password_min_length": settings.password_min_length,
            # A forced user came here trying to reach the app, so put them in
            # it once the flag is cleared. A voluntary visitor asked for this
            # page and stays on it.
            "change_password_redirect": "/" if forced else "",
        },
    )


@router.post("/my/preferences/", response_model=None)
async def update_preferences(
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: AsyncSession = Depends(get_db),  # noqa: B008
    avatar_color: str = Form(""),
    language: str = Form(""),
    timezone: str = Form(""),
) -> Response:
    """Save user preferences (avatar color, language, timezone)."""
    if avatar_color:
        from specivo.services.profile_service import ProfileService

        svc = ProfileService()
        await svc.update_avatar_color(db, user, avatar_color)

    from specivo.core.config import get_settings
    from specivo.core.locales import ALL_TIMEZONES

    settings = get_settings()

    if language and language in settings.available_languages and language != user.language:
        user.language = language

    if timezone and timezone in ALL_TIMEZONES and timezone != user.timezone:
        user.timezone = timezone

    await db.commit()

    return RedirectResponse("/my/preferences/", status_code=303)


@router.post("/my/profile/avatar/", response_model=None)
async def upload_avatar(
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: AsyncSession = Depends(get_db),  # noqa: B008
    file: UploadFile = File(...),
) -> Response:
    """Upload a user avatar photo."""
    from specivo.services.profile_service import ProfileService

    content = await file.read()
    svc = ProfileService()
    await svc.upload_avatar(db, user, content, file.content_type or "", file.filename or "avatar")

    return RedirectResponse("/my/profile/", status_code=303)


@router.post("/my/profile/avatar/delete/", response_model=None)
async def delete_avatar(
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Remove the user avatar photo."""
    from specivo.services.profile_service import ProfileService

    await ProfileService().delete_avatar(db, user)

    return RedirectResponse("/my/profile/", status_code=303)


@router.get("/my/api-keys/", response_model=None)
async def api_keys_page(
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render the API keys management page (requires auth).

    While ``sso.allow_api_keys_for_sso_users`` is off, a user who signs in only
    through SSO gets an explanation instead of the "New API Key" button.
    """
    from specivo.services.api_key_service import sso_api_keys_refused

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/auth/api_keys.html",
        context={
            "user": user,
            "active_page": "api-keys",
            "api_keys_refused": await sso_api_keys_refused(db, user),
        },
    )
