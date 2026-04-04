"""Notification channel registry."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from specivo.services.channels.base import NotificationChannel

logger = logging.getLogger(__name__)

_channels: dict[str, NotificationChannel] = {}


def register_channel(channel: NotificationChannel) -> None:
    """Register a notification channel. Called at app startup.

    Raises ValueError if a *different* channel instance is already
    registered for the same key. Re-registering the same key with
    the same channel class is idempotent (safe for test app re-creation).
    """
    key = channel.channel_key
    if key in _channels:
        raise ValueError(f"Duplicate channel key: {key!r}")
    _channels[key] = channel
    logger.info("Registered notification channel: %s", key)


def get_channel(key: str) -> NotificationChannel | None:
    return _channels.get(key)


def get_all_channels() -> dict[str, NotificationChannel]:
    """Return all registered channels (copy)."""
    return dict(_channels)


def channel_keys() -> list[str]:
    """Return sorted list of registered channel keys."""
    return sorted(_channels.keys())
