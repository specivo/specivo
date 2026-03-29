"""Models for external system credentials and audit logging."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from specivo.models.base import Base, TimestampMixin


class ExternalSystem(Base, TimestampMixin):
    """Registered external system (GitHub, GitLab)."""

    __tablename__ = "external_systems"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    system_type: Mapped[str] = mapped_column(String(30), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class IssuedCredential(Base, TimestampMixin):
    """Temporary credential issued to an agent."""

    __tablename__ = "issued_credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    system_id: Mapped[int] = mapped_column(
        ForeignKey("external_systems.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    scope: Mapped[dict] = mapped_column(JSONB, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CredentialAuditLog(Base):
    """Immutable audit trail for credential operations."""

    __tablename__ = "credential_audit_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    credential_id: Mapped[int | None] = mapped_column(
        ForeignKey("issued_credentials.id", ondelete="SET NULL"), nullable=True
    )
    system_id: Mapped[int] = mapped_column(ForeignKey("external_systems.id", ondelete="CASCADE"), nullable=False)
    agent_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    action: Mapped[str] = mapped_column(String(30), nullable=False)
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
