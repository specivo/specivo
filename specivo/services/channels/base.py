"""Channel abstraction for the notification dispatch layer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class NotificationPayload:
    """Channel-agnostic notification content.

    Built once by NotificationService, consumed by every channel.
    Each channel formats this into its own wire format.
    """

    event_type: str
    entity_type: str
    entity_id: int
    project_id: int
    project_key: str
    actor_name: str
    actor_id: int
    title: str
    body_plain: str
    body_html: str | None
    issue_key: str | None = None
    issue_subject: str | None = None
    comment_text: str | None = None
    entity_url: str | None = None


@runtime_checkable
class NotificationChannel(Protocol):
    """Interface for a notification delivery channel."""

    @property
    def channel_key(self) -> str: ...

    def is_configured_for_user(self, user_channel_config: dict | None) -> bool: ...

    def dispatch(self, payload: NotificationPayload, user_channel_config: dict) -> None: ...
