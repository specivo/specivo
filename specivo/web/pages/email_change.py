"""Changing your email address from the profile, and the page the confirmation link opens.

* :func:`profile_email_context` builds the "Email address" section of
  ``/my/profile/``: the current address, a pending change with **Resend** and
  **Cancel**, the request form (new address and current password), or for an
  account without a password the "Sign in again" buttons of the reauth SSO
  flow (:mod:`specivo.web.pages.sso`). A SCIM-managed account sees its
  address read-only.
* ``POST /my/profile/email/``, ``.../resend/`` and ``.../cancel/`` (signed in,
  CSRF) call :class:`EmailChangeService` and return to the profile with
  ``?email=<outcome>`` or ``?email_error=<code>``; each selects a fixed,
  translated message and is never reflected.
* ``GET /confirm-email/?token=`` shows the change and a **Confirm** button
  and changes nothing, so a mail scanner fetching the link does not use it.
  ``POST /confirm-email/`` makes the change. Neither needs a sign-in: the
  token is the proof, so a link opened in another browser works.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.core.database import DbSession
from specivo.core.exceptions import AppError
from specivo.core.i18n import gettext as _
from specivo.core.rate_limit import rate_limit
from specivo.core.security import request_session, request_session_id
from specivo.models.user import User
from specivo.services.email_change_service import EmailChangeService, email_change_error_message
from specivo.web.deps import get_templates, require_user

router = APIRouter(tags=["web-auth"], include_in_schema=False)
_service = EmailChangeService()

PROFILE_PATH = "/my/profile/"
_confirm_rate_limit = rate_limit("web_confirm_email", max_requests=20, window_seconds=60)


def _prefix() -> str:
    return get_settings().stealth_prefix.rstrip("/")


def _back(**query: str) -> RedirectResponse:
    from urllib.parse import urlencode

    return RedirectResponse(f"{_prefix()}{PROFILE_PATH}?{urlencode(query)}", status_code=303)


def _outcome_messages() -> dict[str, str]:
    return {
        "requested": _(
            "Check the inbox of the new address and open the link to confirm the change. "
            "Your current address stays in use until then."
        ),
        "resent": _("A new link is on its way. Earlier links no longer work."),
        "cancelled": _("The email change was cancelled."),
    }


def _error_code(exc: AppError) -> str:
    return "email_change_address_invalid" if exc.code == "validation_error" else exc.code


async def profile_email_context(
    request: Request,
    db: AsyncSession,
    user: User,
    *,
    outcome: str = "",
    error: str = "",
    reauthenticated: str = "",
) -> dict[str, Any]:
    """Template context of the profile's "Email address" section."""
    from specivo.services.sso.identity_service import IdentityService

    session_id = await request_session_id(request, db, user.id)
    state = await _service.state(db, user, session_id=session_id)
    providers: list[dict[str, str]] = []
    if state.requires_reauth:
        identities = await IdentityService().list_for_user(db, user.id)
        providers = [{"slug": i.provider_slug, "name": i.provider_name} for i in identities if i.provider_enabled]
    message = _outcome_messages().get(outcome)
    if message is None and reauthenticated == "1" and state.reauthenticated:
        message = _("You signed in again. Request the email change within the next five minutes.")
    return {
        "email_state": state,
        "email_reauth_providers": providers,
        "email_message": message,
        "email_error_text": email_change_error_message(error),
        "email_show_section": not state.unavailable or state.managed_by_idp,
    }


@router.post("/my/profile/email/", response_model=None)
async def request_email_change(
    request: Request,
    db: DbSession,
    user: Annotated[User, Depends(require_user)],
    new_email: Annotated[str, Form(max_length=320)] = "",
    current_password: Annotated[str, Form(max_length=1024)] = "",
) -> RedirectResponse:
    """Ask for a change; the same outcome whether or not another account uses the address."""
    session_id = await request_session_id(request, db, user.id)
    try:
        await _service.request_change(
            db, user, new_email=new_email, current_password=current_password or None, session_id=session_id
        )
    except AppError as exc:
        return _back(email_error=_error_code(exc))
    await db.commit()
    return _back(email="requested")


@router.post("/my/profile/email/resend/", response_model=None)
async def resend_email_change(
    db: DbSession,
    user: Annotated[User, Depends(require_user)],
) -> RedirectResponse:
    """Send a new link for the pending change."""
    try:
        await _service.resend(db, user)
    except AppError as exc:
        return _back(email_error=_error_code(exc))
    await db.commit()
    return _back(email="resent")


@router.post("/my/profile/email/cancel/", response_model=None)
async def cancel_email_change(
    db: DbSession,
    user: Annotated[User, Depends(require_user)],
) -> RedirectResponse:
    """Cancel the pending change."""
    await _service.cancel(db, user)
    await db.commit()
    return _back(email="cancelled")


@router.get("/confirm-email/", response_class=HTMLResponse)
async def confirm_email_page(
    request: Request,
    db: DbSession,
    token: Annotated[str, Query(max_length=256)] = "",
) -> HTMLResponse:
    """Show the change the link asks for and a Confirm button; nothing changes on GET."""
    problem, new_email = await _service.inspect(db, token)
    response = get_templates().TemplateResponse(
        request,
        "pages/auth/confirm_email.html",
        context={
            "token": token if problem is None else "",
            "new_email": new_email,
            "error_text": email_change_error_message(problem),
            "done": False,
        },
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@router.post("/confirm-email/", response_class=HTMLResponse)
async def confirm_email(
    request: Request,
    db: DbSession,
    _rl: Annotated[None, Depends(_confirm_rate_limit)],
    token: Annotated[str, Form(min_length=1, max_length=256)],
) -> HTMLResponse:
    """Use the link: change the address and show the result. The token is required (422 without it)."""
    caller = await request_session(request, db)
    error_text: str | None = None
    new_email: str | None = None
    try:
        result = await _service.confirm(
            db,
            token,
            caller_user_id=caller[0] if caller else None,
            caller_session_id=caller[1] if caller else None,
        )
        new_email = result.new_email
        await db.commit()
    except AppError as exc:
        error_text = email_change_error_message(exc.code)
    response = get_templates().TemplateResponse(
        request,
        "pages/auth/confirm_email.html",
        context={"token": "", "new_email": new_email, "error_text": error_text, "done": error_text is None},
        status_code=200 if error_text is None else 400,
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response
