"""Shared utilities."""

from datetime import UTC, datetime


def utcnow() -> datetime:
    """Return timezone-aware UTC datetime. Use this instead of datetime.now(UTC) everywhere."""
    return datetime.now(UTC)
