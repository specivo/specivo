"""Shared utilities."""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    """Return timezone-aware UTC datetime. Use this instead of datetime.now(UTC) everywhere."""
    return datetime.now(UTC)


def safe_int(value: str | int | None, default: int | None = None) -> int | None:
    """Parse *value* as an integer, returning *default* on any failure.

    Safe against empty strings, non-numeric input, and injection attempts.
    Only returns an int if the value is a valid integer literal.
    """
    if value is None:
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default
