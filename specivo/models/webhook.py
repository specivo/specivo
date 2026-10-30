"""Webhook models — outgoing webhook configuration and delivery log."""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from specivo.models.base import Base, TimestampMixin

DELIVERY_PENDING = "pending"
DELIVERY_SUCCEEDED = "succeeded"
DELIVERY_FAILED = "failed"


class Webhook(Base, TimestampMixin):
    """Outgoing webhook configuration.

    ``secret`` is a ``secret_box`` token (purpose ``webhook-secret``).
    ``include_private`` lets the webhook receive private issues and notes.
    """

    __tablename__ = "webhooks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    secret: Mapped[str] = mapped_column(Text, nullable=False)
    events: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    include_private: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")

    def __repr__(self) -> str:
        return f"<Webhook id={self.id} project_id={self.project_id} url={self.url!r}>"


class WebhookDelivery(Base, TimestampMixin):
    """One delivery of one event to one webhook, across all its attempts.

    Rows are written ``pending`` in the transaction that made the change (an
    outbox) and sent after it commits. ``delivery_uuid`` is sent as
    ``X-Specivo-Delivery`` on every attempt so receivers can drop duplicates.
    """

    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        UniqueConstraint("delivery_uuid", name="uq_webhook_deliveries_delivery_uuid"),
        CheckConstraint("status IN ('pending', 'succeeded', 'failed')", name="ck_webhook_deliveries_status"),
        Index(
            "ix_webhook_deliveries_pending_next_attempt",
            "next_attempt_at",
            postgresql_where=text("status = 'pending'"),
        ),
        Index("ix_webhook_deliveries_webhook_id_created_at", "webhook_id", text("created_at DESC")),
        # Retention: the scheduled cleanup deletes settled rows oldest first.
        Index(
            "ix_webhook_deliveries_settled_created_at",
            "created_at",
            postgresql_where=text("status IN ('succeeded', 'failed')"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    webhook_id: Mapped[int] = mapped_column(ForeignKey("webhooks.id", ondelete="CASCADE"), nullable=False, index=True)
    delivery_uuid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, default=uuid.uuid4)
    event: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=DELIVERY_PENDING, server_default=DELIVERY_PENDING
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    def __repr__(self) -> str:
        return f"<WebhookDelivery id={self.id} webhook_id={self.webhook_id} event={self.event!r} status={self.status}>"
