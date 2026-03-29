"""SecurityAuditLog model — immutable append-only audit trail for security events."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from specivo.models.base import Base


class SecurityAuditLog(Base):
    """Immutable audit trail for security-relevant events.

    Events include: access_granted, access_denied, search_query,
    resource_access, auth_failure, permission_change.

    Partitioned by RANGE on created_at (monthly) for efficient pruning.
    """

    __tablename__ = "security_audit_logs"

    __table_args__ = (
        # BRIN index for time-ordered append-only data (same pattern as journals)
        Index("idx_security_audit_created_at_brin", "created_at", postgresql_using="brin"),
        # Standard B-tree indexes for common query patterns
        Index("ix_security_audit_resource", "resource_type", "resource_id"),
        {"postgresql_partition_by": "RANGE (created_at)"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    user_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        index=True,
    )
    resource_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    resource_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    project_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        index=True,
    )
    permission: Mapped[str | None] = mapped_column(String(50), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
