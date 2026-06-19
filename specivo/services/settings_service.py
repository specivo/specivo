"""SettingsService — global application key/value settings."""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
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

    async def get(self, session: AsyncSession, key: str, default: str | None = None) -> str | None:
        """Return a single setting's value, or *default* if unset."""
        result = await session.execute(select(Setting.value).where(Setting.key == key))
        value = result.scalar_one_or_none()
        return value if value is not None else default

    async def get_avatar_palette(self, session: AsyncSession) -> list[str]:
        """Return the avatar color palette from settings, with fallback."""
        import json

        all_settings = await self.get_all(session)
        raw = all_settings.get("avatar_color_palette")
        if raw:
            try:
                palette = json.loads(raw)
                if isinstance(palette, list) and palette:
                    return palette
            except (json.JSONDecodeError, TypeError):
                pass
        from specivo.core.constants import DEFAULT_AVATAR_PALETTE

        return list(DEFAULT_AVATAR_PALETTE)

    async def set_many(self, session: AsyncSession, updates: dict[str, str | None]) -> dict[str, str | None]:
        """Upsert multiple settings.  Returns the full updated settings dict.

        Keys not in ``updates`` are left unchanged.
        A value of ``None`` clears the setting (stored as NULL).
        """
        for key, value in updates.items():
            stmt = pg_insert(Setting).values(key=key, value=value)
            stmt = stmt.on_conflict_do_update(index_elements=["key"], set_={"value": value})
            await session.execute(stmt)

        await session.flush()
        logger.info("Updated settings: %s", list(updates.keys()))
        return await self.get_all(session)

    async def get_dashboard_stats(self, session: AsyncSession) -> dict[str, int]:
        """Return admin dashboard stat counts."""
        from specivo.models.agent_session import AgentSession
        from specivo.models.project import Project
        from specivo.models.user import User

        total_users = (await session.execute(select(func.count()).select_from(User))).scalar_one()
        active_projects = (
            await session.execute(select(func.count()).select_from(Project).where(Project.status == 1))
        ).scalar_one()
        agent_sessions = (await session.execute(select(func.count()).select_from(AgentSession))).scalar_one()

        kill_events = 0
        try:
            from specivo.core.features import has_feature

            if has_feature("kill_switch"):
                from specivo.models.kill_switch import KillEvent

                kill_events = (await session.execute(select(func.count()).select_from(KillEvent))).scalar_one()
        except Exception:
            pass

        return {
            "total_users": total_users,
            "active_projects": active_projects,
            "agent_sessions": agent_sessions,
            "kill_events": kill_events,
        }
