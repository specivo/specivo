"""Pydantic schemas for notification preferences."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class NotificationPreferenceUpdate(BaseModel):
    """Payload for creating or updating a notification preference."""

    event_type: str = Field(min_length=1, max_length=50)
    channels: dict[str, bool] = Field(
        default_factory=dict,
        description='Per-channel enablement, e.g. {"email": true, "in_app": false}',
    )
    project_id: int | None = None


class NotificationPreferenceOut(BaseModel):
    """Response schema for a notification preference."""

    id: int
    user_id: int
    project_id: int | None
    event_type: str
    channels: dict[str, bool]
    created_at: datetime
    updated_at: datetime


class NotificationOut(BaseModel):
    """Response schema for an in-app notification."""

    id: int
    user_id: int
    event_type: str
    entity_type: str
    entity_id: int
    project_id: int
    actor_id: int
    title: str
    body: str | None
    is_read: bool
    read_at: datetime | None
    created_at: datetime


class NotificationListOut(BaseModel):
    """Paginated list of notifications."""

    items: list[NotificationOut]
    total: int
    offset: int
    limit: int


class UnreadCountOut(BaseModel):
    """Unread notification count response."""

    count: int


class MarkAllReadOut(BaseModel):
    """Response for mark-all-read action."""

    marked: int
