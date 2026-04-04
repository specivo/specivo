"""Admin settings API — get and update global application settings."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.api.v1.admin import require_admin_api
from specivo.core.database import get_db
from specivo.models.user import User
from specivo.services.settings_service import SettingsService

router = APIRouter(tags=["admin"])
_service = SettingsService()


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
    Pass ``null`` as a value to clear a setting.
    """
    result = await _service.set_many(db, updates)

    # Update in-memory brand name cache if changed
    if "brand_name" in updates:
        from specivo.web.deps import set_brand_name

        set_brand_name(updates["brand_name"] or "Specivo")

    return result
