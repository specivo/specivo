"""Connected SSO accounts: the profile section's data and the unlink forms.

* :func:`profile_connected_accounts` builds the "Connected accounts" context
  of ``/my/profile/`` (identities, "Connect" buttons, the message for
  ``?sso_linked=``, ``?sso_unlinked=`` or ``?sso_error=``).
* ``POST /my/identities/{id}/unlink/`` disconnects one of the user's own
  identities, while another way to sign in remains.
* ``POST /admin/users/{user_id}/identities/{id}/unlink/`` lets an admin
  disconnect any identity.

Both forms are plain POSTs; the CSRF middleware checks their ``csrf_token``
field. The "Connect" form posts to ``/auth/oidc/{slug}/link/``
(:mod:`specivo.web.pages.sso`).
"""

from __future__ import annotations

from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.core.database import get_db
from specivo.core.exceptions import NotFoundError
from specivo.core.i18n import gettext as _
from specivo.models.sso import SsoProvider
from specivo.models.user import User
from specivo.services.sso.identity_service import IdentityService, LastSignInMethodError
from specivo.web.deps import require_user
from specivo.web.pages.admin import require_admin
from specivo.web.pages.sso import sso_error_message

router = APIRouter(tags=["web-sso"], include_in_schema=False)
_identities = IdentityService()

PROFILE_PATH = "/my/profile/"


def _prefix() -> str:
    return get_settings().stealth_prefix.rstrip("/")


async def profile_connected_accounts(
    db: AsyncSession,
    user: User,
    *,
    linked: str = "",
    unlinked: str = "",
    error: str = "",
) -> dict[str, Any]:
    """Template context of the profile's "Connected accounts" section.

    The query parameters only ever select a fixed, translated message; a
    provider name is shown only for a slug that names a provider the user is
    linked to (``linked``) or an enabled provider (``unlinked``).
    """
    if user.is_service_account:
        return {"sso_enabled": False}
    identities = await _identities.list_for_user(db, user.id)
    linkable = await _identities.linkable_providers(db, user.id)

    message: str | None = None
    if linked:
        name = next((i.provider_name for i in identities if i.provider_slug == linked), None)
        if name is not None:
            message = _("Your %(provider)s account is now connected.") % {"provider": name}
    elif unlinked:
        # Only an enabled provider's name, which the login page shows anyway:
        # the query string is attacker-chosen and must not reveal the names
        # of disabled providers. Unlinking from a disabled one shows no message.
        name = await db.scalar(
            select(SsoProvider.display_name).where(SsoProvider.slug == unlinked, SsoProvider.enabled.is_(True))
        )
        if name is not None:
            message = _("Your %(provider)s account was disconnected.") % {"provider": name}

    return {
        "sso_enabled": bool(identities or linkable),
        "sso_identities": identities,
        "sso_linkable_providers": [{"slug": p.slug, "name": p.display_name} for p in linkable],
        "sso_message": message,
        "sso_error_text": sso_error_message(error),
    }


def _redirect(path: str, **query: str) -> RedirectResponse:
    target = f"{_prefix()}{path}"
    if query:
        target += "?" + urlencode(query)
    return RedirectResponse(target, status_code=303)


@router.post("/my/identities/{identity_id}/unlink/", response_model=None)
async def unlink_own_identity(
    identity_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> RedirectResponse:
    """Disconnect one of the signed-in user's identities and end its sessions."""
    try:
        result = await _identities.unlink(db, user_id=user.id, identity_id=identity_id, by="user", actor_id=user.id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Not found") from None
    except LastSignInMethodError as exc:
        return _redirect(PROFILE_PATH, sso_error=exc.code)
    await db.commit()
    return _redirect(PROFILE_PATH, sso_unlinked=result.provider_slug)


@router.post("/admin/users/{user_id}/identities/{identity_id}/unlink/", response_model=None)
async def admin_unlink_identity(
    request: Request,
    user_id: int,
    identity_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[User, Depends(require_admin)],
) -> RedirectResponse:
    """Disconnect an identity from any user (always allowed for an admin) and end its sessions."""
    try:
        await _identities.unlink(db, user_id=user_id, identity_id=identity_id, by="admin", actor_id=admin.id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Not found") from None
    await db.commit()
    return _redirect(f"/admin/users/{user_id}/")
