"""SettingsService — global application key/value settings."""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.setting import Setting

logger = logging.getLogger(__name__)


class SettingsService:
    """Service layer for reading and updating global settings."""

    async def get_all(self, session: AsyncSession) -> dict[str, str | None]:
        """Return all settings as a key→value dict."""
        result = await session.execute(select(Setting))
        rows = result.scalars().all()
        return {row.key: row.value for row in rows}

    async def set_many(self, session: AsyncSession, updates: dict[str, str | None]) -> dict[str, str | None]:
        """Upsert multiple settings.  Returns the full updated settings dict.

        Keys not in ``updates`` are left unchanged.
        A value of ``None`` clears the setting (stored as NULL).
        """
        for key, value in updates.items():
            result = await session.execute(select(Setting).where(Setting.key == key))
            setting = result.scalar_one_or_none()
            if setting is None:
                setting = Setting(key=key, value=value)
                session.add(setting)
            else:
                setting.value = value

        await session.flush()
        logger.info("Updated settings: %s", list(updates.keys()))
        return await self.get_all(session)
