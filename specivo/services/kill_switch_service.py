"""KillSwitchService — graduated agent access revocation."""

from __future__ import annotations

import logging

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.utils import utcnow
from specivo.models.auth import ApiKey
from specivo.models.credential import IssuedCredential
from specivo.models.kill_switch import KillEvent
from specivo.models.user import User

logger = logging.getLogger(__name__)


class KillSwitchService:
    """Stateless service for kill switch operations."""

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------

    async def _snapshot_agent(self, session: AsyncSession, agent_user_id: int) -> dict:
        """Capture current state of an agent's keys and credentials."""
        # Active API keys
        keys_result = await session.execute(
            select(ApiKey).where(
                ApiKey.user_id == agent_user_id,
                ApiKey.is_active.is_(True),
            )
        )
        keys = keys_result.scalars().all()

        # Active credentials
        creds_result = await session.execute(
            select(IssuedCredential).where(
                IssuedCredential.agent_user_id == agent_user_id,
                IssuedCredential.revoked_at.is_(None),
            )
        )
        creds = creds_result.scalars().all()

        return {
            "api_keys": [{"id": k.id, "name": k.name, "key_prefix": k.key_prefix} for k in keys],
            "credentials": [{"id": c.id, "system_id": c.system_id, "scope": c.scope} for c in creds],
            "snapshot_at": utcnow().isoformat(),
        }

    async def _snapshot_system(self, session: AsyncSession, system_id: int) -> dict:
        """Capture current state of credentials for a system."""
        now = utcnow()
        creds_result = await session.execute(
            select(IssuedCredential).where(
                IssuedCredential.system_id == system_id,
                IssuedCredential.revoked_at.is_(None),
                IssuedCredential.expires_at > now,
            )
        )
        creds = creds_result.scalars().all()

        return {
            "credentials": [
                {
                    "id": c.id,
                    "agent_user_id": c.agent_user_id,
                    "scope": c.scope,
                }
                for c in creds
            ],
            "snapshot_at": utcnow().isoformat(),
        }

    async def _snapshot_all(self, session: AsyncSession) -> dict:
        """Capture current state of all service account keys."""
        keys_result = await session.execute(
            select(ApiKey)
            .join(User, ApiKey.user_id == User.id)
            .where(
                User.is_service_account.is_(True),
                ApiKey.is_active.is_(True),
            )
        )
        keys = keys_result.scalars().all()

        return {
            "api_keys": [{"id": k.id, "user_id": k.user_id, "name": k.name, "key_prefix": k.key_prefix} for k in keys],
            "snapshot_at": utcnow().isoformat(),
        }

    # ------------------------------------------------------------------
    # Kill operations
    # ------------------------------------------------------------------

    async def kill_agent(
        self,
        session: AsyncSession,
        agent_user_id: int,
        triggered_by: str,
        reason: str,
    ) -> KillEvent:
        """Deactivate all API keys for an agent and revoke credentials."""
        snapshot = await self._snapshot_agent(session, agent_user_id)

        # Deactivate all API keys for this agent
        await session.execute(update(ApiKey).where(ApiKey.user_id == agent_user_id).values(is_active=False))

        # Revoke all active credentials for this agent
        now = utcnow()
        await session.execute(
            update(IssuedCredential)
            .where(
                IssuedCredential.agent_user_id == agent_user_id,
                IssuedCredential.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )

        event = KillEvent(
            level="agent",
            target_type="user",
            target_id=agent_user_id,
            triggered_by=triggered_by,
            trigger_reason=reason,
            snapshot=snapshot,
        )
        session.add(event)
        await session.flush()

        logger.warning(
            "KILL SWITCH: agent user_id=%d killed by %s — %s",
            agent_user_id,
            triggered_by,
            reason,
        )
        return event

    async def kill_system(
        self,
        session: AsyncSession,
        system_id: int,
        triggered_by: str,
        reason: str,
    ) -> KillEvent:
        """Revoke all credentials for an external system."""
        snapshot = await self._snapshot_system(session, system_id)

        now = utcnow()
        await session.execute(
            update(IssuedCredential)
            .where(
                IssuedCredential.system_id == system_id,
                IssuedCredential.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )

        event = KillEvent(
            level="system",
            target_type="external_system",
            target_id=system_id,
            triggered_by=triggered_by,
            trigger_reason=reason,
            snapshot=snapshot,
        )
        session.add(event)
        await session.flush()

        logger.warning(
            "KILL SWITCH: system_id=%d killed by %s — %s",
            system_id,
            triggered_by,
            reason,
        )
        return event

    async def kill_all(
        self,
        session: AsyncSession,
        triggered_by: str,
        reason: str,
    ) -> KillEvent:
        """Deactivate ALL service account API keys and revoke ALL credentials."""
        snapshot = await self._snapshot_all(session)

        # Deactivate all API keys belonging to service accounts
        service_account_ids = select(User.id).where(User.is_service_account.is_(True))
        await session.execute(update(ApiKey).where(ApiKey.user_id.in_(service_account_ids)).values(is_active=False))

        # Revoke all active credentials
        now = utcnow()
        await session.execute(
            update(IssuedCredential).where(IssuedCredential.revoked_at.is_(None)).values(revoked_at=now)
        )

        event = KillEvent(
            level="all",
            target_type=None,
            target_id=None,
            triggered_by=triggered_by,
            trigger_reason=reason,
            snapshot=snapshot,
        )
        session.add(event)
        await session.flush()

        logger.warning(
            "KILL SWITCH: ALL agents killed by %s — %s",
            triggered_by,
            reason,
        )
        return event

    # ------------------------------------------------------------------
    # Unkill
    # ------------------------------------------------------------------

    async def unkill_agent(self, session: AsyncSession, agent_user_id: int) -> int:
        """Re-enable all API keys for an agent. Returns count of reactivated keys."""
        result = await session.execute(
            update(ApiKey)
            .where(
                ApiKey.user_id == agent_user_id,
                ApiKey.is_active.is_(False),
            )
            .values(is_active=True)
        )
        await session.flush()

        count = result.rowcount
        logger.info("UNKILL: reactivated %d keys for agent user_id=%d", count, agent_user_id)
        return count

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------

    async def list_kill_events(self, session: AsyncSession) -> list[KillEvent]:
        """List all kill events, newest first."""
        stmt = select(KillEvent).order_by(KillEvent.id.desc())
        result = await session.execute(stmt)
        return list(result.scalars().all())
