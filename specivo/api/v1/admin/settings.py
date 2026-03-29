"""Admin settings API — get and update global application settings."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import get_db
from specivo.core.exceptions import PermissionDeniedError
from specivo.core.security import get_current_user
from specivo.models.user import User
from specivo.services.settings_service import SettingsService

router = APIRouter(tags=["admin"])
_service = SettingsService()


def _require_admin(current_user: User = Depends(get_current_user)) -> User:
    """Dependency: raise 403 if the current user is not an admin."""
    if not current_user.is_admin:
        raise PermissionDeniedError("Admin access required")
    return current_user


@router.get("/admin/settings")
async def get_settings(
    current_user: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str | None]:
    """Return all application settings as a key→value dict (admin only)."""
    return await _service.get_all(db)


@router.patch("/admin/settings")
async def update_settings(
    updates: dict[str, str | None],
    current_user: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str | None]:
    """Upsert one or more settings (admin only).

    Keys not in the request body are left unchanged.
    Pass ``null`` as a value to clear a setting.
    """
    return await _service.set_many(db, updates)
