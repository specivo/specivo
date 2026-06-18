"""Unit tests for recurring-pattern template macros (DB-free)."""

from __future__ import annotations

from datetime import date

import pytest

from specivo.services.recurrence import expand_macros

# A Thursday in Q2 — used across the English assertions.
_D = date(2026, 6, 18)


@pytest.mark.unit
class TestExpandMacros:
    def test_all_macros_english(self) -> None:
        out = expand_macros(
            "{{weekday}} {{day}} {{month}} {{month_num}} {{year}} {{quarter}}", _D, "en"
        )
        assert out == "Thursday 18 June 06 2026 Q2"

    def test_case_insensitive_and_whitespace_tolerant(self) -> None:
        assert expand_macros("{{ YEAR }}/{{Month_Num}}", _D, "en") == "2026/06"

    def test_quarter_boundaries(self) -> None:
        assert expand_macros("{{quarter}}", date(2026, 1, 1), "en") == "Q1"
        assert expand_macros("{{quarter}}", date(2026, 3, 31), "en") == "Q1"
        assert expand_macros("{{quarter}}", date(2026, 4, 1), "en") == "Q2"
        assert expand_macros("{{quarter}}", date(2026, 12, 31), "en") == "Q4"

    def test_localized_names_thai(self) -> None:
        # Stand-alone month/weekday names follow the workspace locale (Thai here,
        # per the project's neutral sample-data convention).
        assert expand_macros("{{month}}", _D, "th") == "มิถุนายน"
        assert expand_macros("{{weekday}}", _D, "th") == "วันพฤหัสบดี"

    def test_bad_locale_falls_back_to_english(self) -> None:
        assert expand_macros("{{month}}", _D, "not-a-locale") == "June"

    def test_unknown_placeholder_left_intact(self) -> None:
        assert expand_macros("{{bogus}} {{year}}", _D, "en") == "{{bogus}} 2026"

    def test_empty_and_none_text(self) -> None:
        assert expand_macros(None, _D, "en") is None
        assert expand_macros("", _D, "en") == ""

    def test_text_without_macros_unchanged(self) -> None:
        assert expand_macros("plain subject", _D, "en") == "plain subject"
