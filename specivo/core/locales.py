"""Locale helpers: language labels and timezone lists.

Provides data for user-facing preference selects. Timezone list comes
from the stdlib ``zoneinfo`` module (IANA tz database). Language labels
are translatable via gettext.
"""

from __future__ import annotations

from pathlib import Path
from zoneinfo import available_timezones

# ---------------------------------------------------------------------------
# Language labels — keyed by ISO 639-1 code
# ---------------------------------------------------------------------------
# Native-name labels. Add entries here when a new translation is available.
# A label here is necessary (but not sufficient) for a language to be offered:
# it must also have a compiled core catalog (see ``get_available_locales``).

LANGUAGE_LABELS: dict[str, str] = {
    "en": "English",
    "ru": "Русский",  # Русский
    "zh": "中文",  # 中文
    "fr": "Français",  # Français
    "es": "Español",  # Español
    "th": "ไทย",  # Thai
}


def get_language_choices(available: list[str]) -> list[tuple[str, str]]:
    """Return ``(code, label)`` pairs for the given language codes."""
    return [(code, LANGUAGE_LABELS.get(code, code)) for code in sorted(available)]


# ---------------------------------------------------------------------------
# Installed locale discovery — the single source of truth for "renderable"
# ---------------------------------------------------------------------------

# Directory holding compiled core catalogs: locale/<code>/LC_MESSAGES/specivo.mo
_LOCALE_DIR = Path(__file__).resolve().parent.parent / "locale"
_CORE_DOMAIN = "specivo"


def get_available_locales() -> list[str]:
    """Return the language codes Specivo can actually render, sorted.

    A code qualifies when it has a compiled core catalog
    (``locale/<code>/LC_MESSAGES/specivo.mo``) **and** a label in
    ``LANGUAGE_LABELS``. ``en`` is always included as the source language
    even though it has no real translations to apply.

    This is the canonical "languages we can offer" list used by config
    defaults, the admin default-language select, and the preferences select.
    """
    codes: set[str] = {"en"}
    if _LOCALE_DIR.is_dir():
        for entry in _LOCALE_DIR.iterdir():
            mo_path = entry / "LC_MESSAGES" / f"{_CORE_DOMAIN}.mo"
            if mo_path.is_file() and entry.name in LANGUAGE_LABELS:
                codes.add(entry.name)
    return sorted(codes & set(LANGUAGE_LABELS))


# ---------------------------------------------------------------------------
# Timezones — full IANA set from the OS tz database
# ---------------------------------------------------------------------------

# Sorted once at import time; frozenset for O(1) membership checks.
ALL_TIMEZONES: frozenset[str] = frozenset(available_timezones())
TIMEZONE_CHOICES: list[str] = sorted(ALL_TIMEZONES)
