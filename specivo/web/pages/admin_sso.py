"""Web admin pages for single sign-on: provider list, provider form, authentication settings form.

The provider form is an Alpine component (``adminSsoProvider``) that talks to
the admin JSON API (``/api/v1/admin/sso/...``); these handlers only render it
with the provider as the API shows it, so the client secret never reaches a
template. The "Authentication" card on ``/admin/settings/`` posts a plain form
to :func:`admin_settings_authentication`.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import get_db
from specivo.core.exceptions import AppError, NotFoundError
from specivo.models.user import User
from specivo.models.user_group import UserGroup
from specivo.schemas.sso import EXTRA_AUTH_PARAM_KEYS, AuthSettingsUpdate
from specivo.services.sso.auth_settings import update_auth_settings
from specivo.services.sso.provider_service import ProviderService, backchannel_logout_uri, redirect_uri
from specivo.web.deps import get_templates
from specivo.web.pages.admin import require_admin

router = APIRouter(tags=["web-admin"], include_in_schema=False)
_service = ProviderService()


async def _user_groups(db: AsyncSession) -> list[dict[str, object]]:
    rows = await db.execute(select(UserGroup.id, UserGroup.name).order_by(UserGroup.name))
    return [{"id": gid, "name": name} for gid, name in rows.all()]


@router.get("/admin/sso/", response_class=HTMLResponse)
async def admin_sso_providers(
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """List the configured identity providers."""
    providers, _total = await _service.list_providers(db)
    counts = await _service.identity_counts(db, [p.id for p in providers])
    items = [_service.to_out(p, identity_count=counts.get(p.id, 0)) for p in providers]
    return get_templates().TemplateResponse(
        request,
        "pages/admin/sso_providers.html",
        context={"user": user, "active_page": "admin", "providers": items},
    )


def _render_form(request: Request, user: User, provider: dict[str, object] | None, groups, mappings) -> Response:
    return get_templates().TemplateResponse(
        request,
        "pages/admin/sso_provider_detail.html",
        context={
            "user": user,
            "active_page": "admin",
            "provider": provider,
            "user_groups": groups,
            "group_mappings": mappings,
            "extra_param_keys": sorted(EXTRA_AUTH_PARAM_KEYS),
            # For the URL preview while the slug of a new provider is typed.
            "callback_template": redirect_uri("__slug__"),
            "backchannel_template": backchannel_logout_uri("__slug__"),
        },
    )


@router.get("/admin/sso/new/", response_class=HTMLResponse)
async def admin_sso_provider_new(
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render the empty provider form."""
    return _render_form(request, user, None, await _user_groups(db), [])


@router.get("/admin/sso/{provider_id}/", response_class=HTMLResponse)
async def admin_sso_provider_detail(
    request: Request,
    provider_id: int,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render one provider's form, its URLs to copy, and its group mappings."""
    try:
        provider = await _service.get(db, provider_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="SSO provider not found") from None
    out = await _service.out(db, provider)
    mappings = await _service.list_group_mappings(db, provider_id)
    return _render_form(
        request,
        user,
        out.model_dump(mode="json"),
        await _user_groups(db),
        [m.model_dump() for m in mappings],
    )


@router.post("/admin/settings/authentication/", response_model=None)
async def admin_settings_authentication(
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),  # noqa: B008
    hide_local_login: str = Form(""),
    local_login_admins_only: str = Form(""),
    auto_redirect_provider: str = Form(""),
    allow_api_keys_for_sso_users: str = Form(""),
) -> Response:
    """Save the authentication card. Unchecked boxes are absent from the form and mean off."""
    flags: dict[str, Any] = {
        "hide_local_login": hide_local_login == "1",
        "local_login_admins_only": local_login_admins_only == "1",
        "allow_api_keys_for_sso_users": allow_api_keys_for_sso_users == "1",
    }
    try:
        data = AuthSettingsUpdate(**flags, auto_redirect_provider=auto_redirect_provider.strip() or None)
        await update_auth_settings(db, data, actor=user)
    except (AppError, PydanticValidationError):
        # The select only offers existing providers: a stale or forged choice
        # is dropped and the rest of the card is still saved.
        await update_auth_settings(db, AuthSettingsUpdate(**flags), actor=user)
    await db.commit()
    return RedirectResponse("/admin/settings/#authentication", status_code=303)
