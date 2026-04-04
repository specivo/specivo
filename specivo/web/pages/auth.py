"""Web auth pages: login, logout, password reset, API keys management."""

from __future__ import annotations

import hashlib
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import get_db
from specivo.core.utils import utcnow
from specivo.models.user import User
from specivo.web.deps import get_current_user_optional, get_templates, require_user

logger = logging.getLogger(__name__)

router = APIRouter(tags=["web-auth"], include_in_schema=False)


@router.get("/login/", response_class=HTMLResponse)
async def login_page(
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
    reset: str = Query(""),  # noqa: B008
) -> HTMLResponse:
    """Render the login page (standalone layout, no sidebar).

    If the user is already authenticated, the template shows a
    'You are logged in' widget instead of the login form.
    Accepts ``?reset=ok`` query param to show a success banner after password reset.
    """
    user = await get_current_user_optional(request, db)
    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/auth/login.html",
        context={"user": user, "reset_ok": reset == "ok"},
    )


@router.get("/logout/")
async def logout_page() -> RedirectResponse:
    """Clear auth cookies and redirect to login."""
    response = RedirectResponse("/login/", status_code=302)
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
) -> Response:
    """Render the user profile page."""
    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/auth/profile.html",
        context={"user": user, "active_page": "profile"},
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
    from specivo.services.settings_service import SettingsService

    settings = get_settings()
    palette = await SettingsService().get_avatar_palette(db)
    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/auth/preferences.html",
        context={
            "user": user,
            "active_page": "preferences",
            "avatar_palette": palette,
            "language_choices": get_language_choices(settings.available_languages),
            "timezone_choices": TIMEZONE_CHOICES,
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
) -> Response:
    """Render the API keys management page (requires auth)."""
    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/auth/api_keys.html",
        context={"user": user, "active_page": "api-keys"},
    )
