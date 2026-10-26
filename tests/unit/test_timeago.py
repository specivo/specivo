"""Unit tests for the timeago Jinja2 filter.

Every test passes an explicit ``now`` so the result never depends on the wall
clock: the subject is the formatting rule, not the current time.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from specivo.web.deps import _timeago

# Mid-afternoon UTC, so the plain cases sit well away from any day boundary.
NOW = datetime(2026, 6, 10, 15, 30, tzinfo=UTC)


@pytest.mark.unit
class TestTimeagoSmart:
    """Default 'smart' mode: under 24h → relative, previous day → Yesterday, older → date."""

    def test_just_now(self):
        assert _timeago(NOW, now=NOW) == "just now"

    def test_minutes_ago(self):
        assert _timeago(NOW - timedelta(minutes=5), now=NOW) == "5 min ago"

    def test_hours_ago(self):
        assert _timeago(NOW - timedelta(hours=3), now=NOW) == "3 hrs ago"

    def test_one_hour_ago(self):
        assert _timeago(NOW - timedelta(hours=1), now=NOW) == "1 hr ago"

    def test_previous_day_under_24h_is_relative(self):
        # 23:00 the day before, 16.5 hours earlier: recent, so still relative.
        t = datetime(2026, 6, 9, 23, 0, tzinfo=UTC)
        assert _timeago(t, now=NOW) == "16 hrs ago"

    def test_yesterday(self):
        assert _timeago(NOW - timedelta(days=1), now=NOW) == "Yesterday"

    def test_older_same_year(self):
        t = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
        assert _timeago(t, now=NOW) == "Jan 15"

    def test_older_previous_year(self):
        t = datetime(2025, 11, 3, 12, 0, tzinfo=UTC)
        assert _timeago(t, now=NOW) == "Nov 03, 2025"

    def test_none_returns_empty(self):
        assert _timeago(None, now=NOW) == ""

    def test_naive_datetime_treated_as_utc(self):
        t = NOW.replace(tzinfo=None) - timedelta(hours=1)
        assert _timeago(t, now=NOW) == "1 hr ago"

    def test_naive_now_treated_as_utc(self):
        t = NOW - timedelta(minutes=5)
        assert _timeago(t, now=NOW.replace(tzinfo=None)) == "5 min ago"

    def test_defaults_to_current_time(self):
        assert _timeago(datetime.now(UTC)) == "just now"


@pytest.mark.unit
class TestTimeagoRelative:
    """'relative' mode: always shows relative time."""

    def test_minutes(self):
        assert _timeago(NOW - timedelta(minutes=5), mode="relative", now=NOW) == "5 min ago"

    def test_hours(self):
        assert _timeago(NOW - timedelta(hours=3), mode="relative", now=NOW) == "3 hrs ago"

    def test_days(self):
        assert _timeago(NOW - timedelta(days=4), mode="relative", now=NOW) == "4 days ago"

    def test_weeks(self):
        assert _timeago(NOW - timedelta(days=14), mode="relative", now=NOW) == "2 weeks ago"

    def test_months(self):
        assert _timeago(NOW - timedelta(days=90), mode="relative", now=NOW) == "3 months ago"


@pytest.mark.unit
class TestTimeagoDate:
    """'date' mode: always shows date."""

    def test_today_shows_date(self):
        assert _timeago(NOW, mode="date", now=NOW) == "Jun 10"
