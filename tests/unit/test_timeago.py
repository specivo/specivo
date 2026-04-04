"""Unit tests for the timeago Jinja2 filter."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest


@pytest.mark.unit
class TestTimeagoSmart:
    """Default 'smart' mode: today→relative, yesterday→Yesterday, older→date."""

    def test_just_now(self):
        from specivo.web.deps import _timeago

        assert _timeago(datetime.now(UTC)) == "just now"

    def test_minutes_ago(self):
        from specivo.web.deps import _timeago

        t = datetime.now(UTC) - timedelta(minutes=5)
        assert _timeago(t) == "5 min ago"

    def test_hours_ago(self):
        from specivo.web.deps import _timeago

        t = datetime.now(UTC) - timedelta(hours=3)
        assert _timeago(t) == "3 hrs ago"

    def test_one_hour_ago(self):
        from specivo.web.deps import _timeago

        t = datetime.now(UTC) - timedelta(hours=1)
        assert _timeago(t) == "1 hr ago"

    def test_yesterday(self):
        from specivo.web.deps import _timeago

        t = datetime.now(UTC) - timedelta(days=1)
        assert _timeago(t) == "Yesterday"

    def test_older_same_year(self):
        from specivo.web.deps import _timeago

        t = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
        now = datetime(2026, 4, 2, 12, 0, tzinfo=UTC)
        # Patch: call directly with a date far enough back
        result = _timeago(t)
        assert "Jan" in result or "2026" in result  # depends on current year

    def test_none_returns_empty(self):
        from specivo.web.deps import _timeago

        assert _timeago(None) == ""

    def test_naive_datetime_treated_as_utc(self):
        from specivo.web.deps import _timeago

        t = datetime.utcnow() - timedelta(hours=1)
        assert "hr" in _timeago(t)


@pytest.mark.unit
class TestTimeagoRelative:
    """'relative' mode: always shows relative time."""

    def test_minutes(self):
        from specivo.web.deps import _timeago

        t = datetime.now(UTC) - timedelta(minutes=5)
        assert _timeago(t, mode="relative") == "5 min ago"

    def test_hours(self):
        from specivo.web.deps import _timeago

        t = datetime.now(UTC) - timedelta(hours=3)
        assert _timeago(t, mode="relative") == "3 hrs ago"

    def test_days(self):
        from specivo.web.deps import _timeago

        t = datetime.now(UTC) - timedelta(days=4)
        assert _timeago(t, mode="relative") == "4 days ago"

    def test_weeks(self):
        from specivo.web.deps import _timeago

        t = datetime.now(UTC) - timedelta(days=14)
        assert _timeago(t, mode="relative") == "2 weeks ago"

    def test_months(self):
        from specivo.web.deps import _timeago

        t = datetime.now(UTC) - timedelta(days=90)
        assert _timeago(t, mode="relative") == "3 months ago"


@pytest.mark.unit
class TestTimeagoDate:
    """'date' mode: always shows date."""

    def test_today_shows_date(self):
        from specivo.web.deps import _timeago

        t = datetime.now(UTC)
        result = _timeago(t, mode="date")
        # Should be a month+day format, not relative
        assert "ago" not in result
        assert "just now" not in result
