"""Notification models — in-app notifications, preferences, and channel configs."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from specivo.models.base import Base, TimestampMixin


class NotificationPreference(Base, TimestampMixin):
    """User preference for notification channels.

    Each row controls which channels are enabled for a specific event type,
    optionally scoped to a project. The ``channels`` JSONB column stores
    per-channel enablement: ``{"email": true, "in_app": false}``.

    When no row exists for a (user, project, event_type) triple, all channels
    default to enabled.

    Event types:
    - ``assignment`` — issue assigned to the user
    - ``comment`` — new comment on a watched issue
    - ``status_change`` — status changed on a watched issue
    - ``mention`` — user mentioned in a comment
    """

    __tablename__ = "notification_preferences"

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "project_id",
            "event_type",
            name="uq_notification_pref",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    event_type: Mapped[str] = mapped_column(String(50), nullable=False)

    channels: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        server_default="{}",
    )


class Notification(Base, TimestampMixin):
    """In-app notification record.

    Each row represents a single notification delivered to a user's inbox.
    Notifications are created by the NotificationService alongside channel
    notifications, controlled by the ``in_app`` key in the channels preference.
    """

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    event_type: Mapped[str] = mapped_column(String(50), nullable=False)

    entity_type: Mapped[str] = mapped_column(String(30), nullable=False)

    entity_id: Mapped[int] = mapped_column(Integer, nullable=False)

    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    actor_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    title: Mapped[str] = mapped_column(String(500), nullable=False)

    body: Mapped[str | None] = mapped_column(Text, nullable=True)

    is_read: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )

    read_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class NotificationChannelConfig(Base, TimestampMixin):
    """Per-user configuration for a notification channel.

    Stores channel-specific credentials/identifiers:
    - telegram: {"chat_id": "123456789"}
    - discord: {"user_id": "987654321", "webhook_url": "..."}

    Email does not need a row here — it reads from User.email directly.
    """

    __tablename__ = "notification_channel_configs"

    __table_args__ = (UniqueConstraint("user_id", "channel_key", name="uq_user_channel"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    channel_key: Mapped[str] = mapped_column(String(30), nullable=False)

    config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    is_verified: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
