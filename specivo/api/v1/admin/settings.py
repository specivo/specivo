"""Admin settings API — global application settings and the anonymous access switch."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.api.v1.admin import require_admin_api
from specivo.core.database import get_db
from specivo.core.exceptions import AppError
from specivo.models.user import User
from specivo.schemas.anonymous_access import AnonymousAccessOut, AnonymousAccessProject, AnonymousAccessUpdate
from specivo.services.anonymous_access_service import (
    ANONYMOUS_ACCESS_SETTING_KEY,
    is_anonymous_access_enabled,
    list_projects_with_anonymous_permissions,
    set_anonymous_access_enabled,
)
from specivo.services.settings_service import SettingsService

router = APIRouter(tags=["admin"])
_service = SettingsService()

# Settings with their own endpoint, because changing them needs confirmation
# or an audit entry the generic key/value PATCH cannot provide.
_MANAGED_SETTINGS = {ANONYMOUS_ACCESS_SETTING_KEY: "/api/v1/admin/settings/anonymous-access/"}


@router.get("/admin/settings/")
async def get_settings(
    current_user: User = Depends(require_admin_api),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str | None]:
    """Return all application settings as a key→value dict (admin only)."""
    return await _service.get_all(db)


@router.patch("/admin/settings/")
async def update_settings(
    updates: dict[str, str | None],
    current_user: User = Depends(require_admin_api),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str | None]:
    """Upsert one or more settings (admin only).

    Keys not in the request body are left unchanged.
    Pass ``null`` as a value to clear a setting. Settings that have their own
    endpoint (the anonymous access switch) are refused with 422.
    """
    for key in sorted(updates):
        if key in _MANAGED_SETTINGS:
            raise AppError(
                code="setting_managed_elsewhere",
                message=f"'{key}' can only be changed through {_MANAGED_SETTINGS[key]}",
                status_code=422,
                field=key,
            )
    result = await _service.set_many(db, updates)

    # Update in-memory brand name cache if changed
    if "brand_name" in updates:
        from specivo.web.deps import set_brand_name

        set_brand_name(updates["brand_name"] or "Specivo")

    return result


async def _anonymous_access_out(db: AsyncSession) -> AnonymousAccessOut:
    projects = await list_projects_with_anonymous_permissions(db)
    return AnonymousAccessOut(
        enabled=await is_anonymous_access_enabled(db),
        projects=[
            AnonymousAccessProject(key=p.key, name=p.name, anonymous_permissions=list(p.anonymous_permissions))
            for p in projects
        ],
    )


@router.get("/admin/settings/anonymous-access/", response_model=AnonymousAccessOut)
async def get_anonymous_access(
    current_user: User = Depends(require_admin_api),
    db: AsyncSession = Depends(get_db),
) -> AnonymousAccessOut:
    """Return the anonymous access switch and the projects opted in to it (admin only)."""
    return await _anonymous_access_out(db)


@router.patch("/admin/settings/anonymous-access/", response_model=AnonymousAccessOut)
async def update_anonymous_access(
    data: AnonymousAccessUpdate,
    request: Request,
    current_user: User = Depends(require_admin_api),
    db: AsyncSession = Depends(get_db),
) -> AnonymousAccessOut:
    """Turn the anonymous access switch on or off (admin only).

    The switch alone exposes nothing: it lets the public projects that were
    individually opted in be read without an account. Turning it on requires
    ``confirm: true``; without it the response is 409 ``confirmation_required``
    and ``details.projects`` names the projects that would become readable.
    Every change is written to the security audit log.
    """
    await set_anonymous_access_enabled(db, data.enabled, current_user, confirmed=data.confirm, request=request)
    await db.commit()
    return await _anonymous_access_out(db)
