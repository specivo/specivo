"""Runtime-mutable settings cache.

Holds workspace settings that admins can change at runtime without a
restart. Unlike ``core.config.Settings`` (immutable, env-driven), these
values live in a module global and are seeded from the DB at startup,
then updated by admin handlers.

Currently holds the workspace-wide default language override
(DB key ``default_language``). ``None`` means "no override" — the
resolver then falls back to ``settings.default_language``.
"""

from __future__ import annotations

_default_language_override: str | None = None


def get_default_language_override() -> str | None:
    """Return the admin-configured default language, or ``None`` if unset."""
    return _default_language_override


def set_default_language_override(value: str | None) -> None:
    """Set (or clear, with ``None``) the workspace default language override."""
    global _default_language_override
    _default_language_override = value
