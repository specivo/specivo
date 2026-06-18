"""Template macros for recurring-pattern subjects and descriptions.

A pattern's ``template_subject`` / ``template_description`` may contain
``{{macro}}`` placeholders that expand, per generated occurrence, to that
occurrence's local calendar date (in the pattern's timezone). This keeps
generated issues distinct instead of identical duplicates, e.g.::

    "{{weekday}} standup — {{day}} {{month}} {{year}}"
        -> "Thursday standup — 18 June 2026"

Month and weekday names are localized to the workspace language via Babel's
CLDR data (stand-alone forms: ``LLLL`` / ``cccc``). The function is pure and
DB-free, so it is exhaustively unit-testable.
"""

from __future__ import annotations

import re
from datetime import date

from babel.dates import format_date

# {{ name }} — case-insensitive, tolerant of surrounding whitespace.
_MACRO_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def _values(local_date: date, locale: str) -> dict[str, str]:
    """Build the macro -> value mapping for one occurrence's local date."""

    def _localized(skeleton: str) -> str:
        # Fall back to English on an unknown/invalid locale rather than raising.
        try:
            return format_date(local_date, skeleton, locale=locale)
        except Exception:
            return format_date(local_date, skeleton, locale="en")

    return {
        "year": str(local_date.year),
        "quarter": f"Q{(local_date.month - 1) // 3 + 1}",
        "month": _localized("LLLL"),  # stand-alone month name (e.g. "июнь")
        "month_num": f"{local_date.month:02d}",
        "day": f"{local_date.day:02d}",
        "weekday": _localized("cccc"),  # stand-alone weekday name
    }


def expand_macros(text: str | None, local_date: date, locale: str = "en") -> str | None:
    """Expand ``{{macro}}`` placeholders in *text* for *local_date*.

    Matching is case-insensitive and whitespace-tolerant. Unknown placeholders
    are left untouched (never silently dropped). Returns *text* unchanged when
    it is empty/None.
    """
    if not text:
        return text

    values = _values(local_date, locale)

    def _sub(match: re.Match[str]) -> str:
        key = match.group(1).lower()
        # Unknown macro: preserve the original placeholder verbatim.
        return values.get(key, match.group(0))

    return _MACRO_RE.sub(_sub, text)
