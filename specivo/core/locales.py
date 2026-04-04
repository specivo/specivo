"""Locale helpers: language labels and timezone lists.

Provides data for user-facing preference selects. Timezone list comes
from the stdlib ``zoneinfo`` module (IANA tz database). Language labels
are translatable via gettext.
"""

from __future__ import annotations

from zoneinfo import available_timezones

# ---------------------------------------------------------------------------
# Language labels — keyed by ISO 639-1 code
# ---------------------------------------------------------------------------
# Add entries here when a new translation is available.

LANGUAGE_LABELS: dict[str, str] = {
    "en": "English",
    "th": "\u0e44\u0e17\u0e22",  # ไทย
}


def get_language_choices(available: list[str]) -> list[tuple[str, str]]:
    """Return ``(code, label)`` pairs for the given language codes."""
    return [(code, LANGUAGE_LABELS.get(code, code)) for code in sorted(available)]


# ---------------------------------------------------------------------------
# Timezones — full IANA set from the OS tz database
# ---------------------------------------------------------------------------

# Sorted once at import time; frozenset for O(1) membership checks.
ALL_TIMEZONES: frozenset[str] = frozenset(available_timezones())
TIMEZONE_CHOICES: list[str] = sorted(ALL_TIMEZONES)
