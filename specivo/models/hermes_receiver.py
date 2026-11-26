"""Hermes event receivers: where a Hermes Agent instance posts its signed outbound webhooks.

A receiver belongs to one account (``user_id``): every event it accepts is
recorded as that account's agent session activity. ``token`` is the URL part
that names the receiver; ``secret`` is the HMAC key Hermes signs each body with,
stored as a ``secret_box`` token (purpose ``hermes-receiver-secret``). Both are
shown once, when the receiver is created (the secret again when it is rotated).

``killed_at`` is set by the kill switch (``KillSwitchService.kill_agent`` /
``kill_all``) and cleared by ``unkill_agent``. It is kept apart from
``is_active``, the owner's own switch, so lifting a kill never turns on a
receiver its owner had switched off, and switching it off and on again does not
lift a kill. A receiver accepts events only while active and not killed.

``hermes_deliveries`` remembers the delivery ids a receiver has accepted, so a
redelivered event is answered ``duplicate`` and written once. Rows older than
``HERMES_DELIVERY_RETENTION_DAYS`` are pruned by the cleanup task.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from specivo.models.base import Base, TimestampMixin


class HermesReceiver(Base, TimestampMixin):
    """One Hermes instance or profile that posts its events to Specivo."""

    __tablename__ = "hermes_receivers"
    __table_args__ = (
        Index("ix_hermes_receivers_user_id", "user_id"),
        Index("ix_hermes_receivers_created_by_id", "created_by_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    token: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    secret: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set by the kill switch, cleared by unkill; independent of the owner's own ``is_active``.
    killed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    def __repr__(self) -> str:
        return f"<HermesReceiver id={self.id} user_id={self.user_id} name={self.name!r}>"


class HermesDelivery(Base):
    """A delivery id a receiver has accepted, kept for redelivery checks."""

    __tablename__ = "hermes_deliveries"
    __table_args__ = (Index("ix_hermes_deliveries_received_at", "received_at"),)

    receiver_id: Mapped[int] = mapped_column(
        ForeignKey("hermes_receivers.id", ondelete="CASCADE"), primary_key=True, nullable=False
    )
    delivery_id: Mapped[str] = mapped_column(String(64), primary_key=True, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
