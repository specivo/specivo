"""CredentialBrokerService — manage external systems, issue/revoke credentials, audit."""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.constants import CREDENTIAL_TOKEN_ENTROPY_BYTES
from specivo.core.exceptions import ConflictError, NotFoundError
from specivo.core.utils import utcnow
from specivo.models.credential import CredentialAuditLog, ExternalSystem, IssuedCredential

logger = logging.getLogger(__name__)


class CredentialBrokerService:
    """Stateless service for credential brokering."""

    # ------------------------------------------------------------------
    # External Systems
    # ------------------------------------------------------------------

    async def register_system(
        self,
        session: AsyncSession,
        system_type: str,
        name: str,
        config: dict,
    ) -> ExternalSystem:
        """Register a new external system."""
        existing = await session.execute(select(ExternalSystem).where(ExternalSystem.name == name))
        if existing.scalar_one_or_none() is not None:
            raise ConflictError(f"External system '{name}' already exists")

        system = ExternalSystem(
            system_type=system_type,
            name=name,
            config=config,
            is_active=True,
        )
        session.add(system)
        await session.flush()
        logger.info("Registered external system %d: %s (%s)", system.id, name, system_type)
        return system

    async def list_systems(self, session: AsyncSession) -> list[ExternalSystem]:
        """List all external systems ordered by name."""
        stmt = select(ExternalSystem).order_by(ExternalSystem.name)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def delete_system(self, session: AsyncSession, system_id: int) -> None:
        """Delete an external system by ID."""
        system = await session.get(ExternalSystem, system_id)
        if system is None:
            raise NotFoundError(f"External system {system_id} not found")
        await session.delete(system)
        await session.flush()
        logger.info("Deleted external system %d", system_id)

    # ------------------------------------------------------------------
    # Credential Issuance
    # ------------------------------------------------------------------

    @staticmethod
    def _hash_token(raw_token: str) -> str:
        """Return SHA-256 hex digest of a raw token."""
        return hashlib.sha256(raw_token.encode()).hexdigest()

    async def issue_credential(
        self,
        session: AsyncSession,
        system_id: int,
        agent_user_id: int,
        scope: dict,
        ttl_minutes: int = 60,
    ) -> tuple[IssuedCredential, str]:
        """Issue a temporary credential.

        Returns (IssuedCredential, raw_token). The raw token is shown once
        and never stored — only the SHA-256 hash is persisted.
        """
        # Verify system exists
        system = await session.get(ExternalSystem, system_id)
        if system is None:
            raise NotFoundError(f"External system {system_id} not found")

        raw_token = secrets.token_urlsafe(CREDENTIAL_TOKEN_ENTROPY_BYTES)
        token_hash = self._hash_token(raw_token)
        expires_at = utcnow() + timedelta(minutes=ttl_minutes)

        cred = IssuedCredential(
            system_id=system_id,
            agent_user_id=agent_user_id,
            scope=scope,
            token_hash=token_hash,
            expires_at=expires_at,
        )
        session.add(cred)
        await session.flush()

        # Audit log
        audit = CredentialAuditLog(
            credential_id=cred.id,
            system_id=system_id,
            agent_user_id=agent_user_id,
            action="issued",
            details={"scope": scope, "ttl_minutes": ttl_minutes},
        )
        session.add(audit)
        await session.flush()

        logger.info(
            "Issued credential %d for agent %d on system %d (TTL=%dm)",
            cred.id,
            agent_user_id,
            system_id,
            ttl_minutes,
        )
        return cred, raw_token

    # ------------------------------------------------------------------
    # Revocation
    # ------------------------------------------------------------------

    async def revoke_credential(
        self,
        session: AsyncSession,
        credential_id: int,
        actor_user_id: int,
    ) -> None:
        """Revoke a credential immediately."""
        cred = await session.get(IssuedCredential, credential_id)
        if cred is None:
            raise NotFoundError(f"Credential {credential_id} not found")

        now = utcnow()
        cred.revoked_at = now

        # Audit log
        audit = CredentialAuditLog(
            credential_id=cred.id,
            system_id=cred.system_id,
            agent_user_id=cred.agent_user_id,
            action="revoked",
            details={"revoked_by": actor_user_id},
        )
        session.add(audit)
        await session.flush()

        logger.info("Revoked credential %d by user %d", credential_id, actor_user_id)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    async def validate_credential(
        self,
        session: AsyncSession,
        raw_token: str,
    ) -> IssuedCredential | None:
        """Validate a raw token. Returns the credential if valid, None otherwise."""
        token_hash = self._hash_token(raw_token)
        now = utcnow()

        stmt = select(IssuedCredential).where(
            IssuedCredential.token_hash == token_hash,
            IssuedCredential.expires_at > now,
            IssuedCredential.revoked_at.is_(None),
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------

    async def list_active_credentials(
        self,
        session: AsyncSession,
        system_id: int | None = None,
    ) -> list[IssuedCredential]:
        """List active (non-expired, non-revoked) credentials."""
        now = utcnow()
        stmt = select(IssuedCredential).where(
            IssuedCredential.expires_at > now,
            IssuedCredential.revoked_at.is_(None),
        )
        if system_id is not None:
            stmt = stmt.where(IssuedCredential.system_id == system_id)
        stmt = stmt.order_by(IssuedCredential.id)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def list_audit_logs(
        self,
        session: AsyncSession,
        system_id: int | None = None,
        agent_user_id: int | None = None,
    ) -> list[CredentialAuditLog]:
        """List audit log entries with optional filters."""
        stmt = select(CredentialAuditLog).order_by(CredentialAuditLog.id.desc())
        if system_id is not None:
            stmt = stmt.where(CredentialAuditLog.system_id == system_id)
        if agent_user_id is not None:
            stmt = stmt.where(CredentialAuditLog.agent_user_id == agent_user_id)
        result = await session.execute(stmt)
        return list(result.scalars().all())
