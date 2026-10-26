"""Unit tests for timezone-aware Jinja2 filters: localtime, localdt, timeago with tz."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest


@pytest.mark.unit
class TestToUserTz:
    """Internal _to_user_tz helper converts UTC datetimes to user timezone."""

    def test_utc_to_bangkok(self):
        from specivo.web.deps import _to_user_tz

        dt = datetime(2026, 4, 9, 23, 24, tzinfo=UTC)
        result = _to_user_tz(dt, "Asia/Bangkok")
        assert result.hour == 6
        assert result.day == 10

    def test_utc_stays_utc(self):
        from specivo.web.deps import _to_user_tz

        dt = datetime(2026, 4, 9, 23, 24, tzinfo=UTC)
        result = _to_user_tz(dt, "UTC")
        assert result.hour == 23
        assert result.day == 9

    def test_none_returns_none(self):
        from specivo.web.deps import _to_user_tz

        assert _to_user_tz(None, "Asia/Bangkok") is None

    def test_naive_treated_as_utc(self):
        from specivo.web.deps import _to_user_tz

        dt = datetime(2026, 4, 9, 23, 24)
        result = _to_user_tz(dt, "Asia/Bangkok")
        assert result.hour == 6
        assert result.day == 10

    def test_invalid_tz_falls_back_to_utc(self):
        from specivo.web.deps import _to_user_tz

        dt = datetime(2026, 4, 9, 23, 24, tzinfo=UTC)
        result = _to_user_tz(dt, "Invalid/Timezone")
        assert result.hour == 23

    def test_empty_tz_falls_back_to_utc(self):
        from specivo.web.deps import _to_user_tz

        dt = datetime(2026, 4, 9, 23, 24, tzinfo=UTC)
        result = _to_user_tz(dt, "")
        assert result.hour == 23


@pytest.mark.unit
class TestLocaltime:
    """The localtime filter formats as YYYY-MM-DD HH:MM in user timezone."""

    def test_bangkok_timezone(self):
        from specivo.web.deps import _localtime

        dt = datetime(2026, 4, 9, 23, 24, tzinfo=UTC)
        assert _localtime(dt, "Asia/Bangkok") == "2026-04-10 06:24"

    def test_utc_default(self):
        from specivo.web.deps import _localtime

        dt = datetime(2026, 4, 9, 23, 24, tzinfo=UTC)
        assert _localtime(dt) == "2026-04-09 23:24"

    def test_none_returns_empty(self):
        from specivo.web.deps import _localtime

        assert _localtime(None) == ""

    def test_us_eastern(self):
        from specivo.web.deps import _localtime

        dt = datetime(2026, 4, 9, 23, 24, tzinfo=UTC)
        # EDT is UTC-4 in April
        assert _localtime(dt, "America/New_York") == "2026-04-09 19:24"


@pytest.mark.unit
class TestLocaldt:
    """The localdt filter formats in the user timezone, for the active locale."""

    def test_default_style_is_date_and_time(self):
        from specivo.web.deps import _localdt

        dt = datetime(2026, 4, 9, 23, 24, tzinfo=UTC)
        assert _localdt(dt, "Asia/Bangkok") == "Apr 10, 2026, 06:24"

    def test_skeleton_style(self):
        from specivo.web.deps import _localdt

        dt = datetime(2026, 4, 9, 23, 24, tzinfo=UTC)
        assert _localdt(dt, "Asia/Bangkok", "yMMMd") == "Apr 10, 2026"

    def test_none_returns_empty(self):
        from specivo.web.deps import _localdt

        assert _localdt(None, "UTC", "yMMMd") == ""


@pytest.mark.unit
class TestTimeagoTimezone:
    """timeago filter respects user timezone for date boundaries."""

    # 17:03 UTC is 00:03 the next day in Bangkok (UTC+7).
    BANGKOK_JUST_AFTER_MIDNIGHT = datetime(2026, 4, 9, 17, 3, tzinfo=UTC)

    def test_today_boundary_bangkok(self):
        """Five minutes before Bangkok midnight is still "5 min ago", not "Yesterday"."""
        from specivo.web.deps import _timeago

        now = self.BANGKOK_JUST_AFTER_MIDNIGHT
        dt = now - timedelta(minutes=5)
        assert _timeago(dt, tz_name="Asia/Bangkok", now=now) == "5 min ago"

    def test_relative_mode_ignores_tz(self):
        """Relative mode produces the same output regardless of timezone."""
        from specivo.web.deps import _timeago

        now = self.BANGKOK_JUST_AFTER_MIDNIGHT
        dt = now - timedelta(hours=3)
        assert _timeago(dt, tz_name="Asia/Bangkok", mode="relative", now=now) == "3 hrs ago"
        assert _timeago(dt, tz_name="UTC", mode="relative", now=now) == "3 hrs ago"

    def test_backward_compat_no_tz(self):
        """Calling without tz_name still works (defaults to UTC)."""
        from specivo.web.deps import _timeago

        now = self.BANGKOK_JUST_AFTER_MIDNIGHT
        assert _timeago(now - timedelta(minutes=5), now=now) == "5 min ago"

    def test_date_mode_uses_user_tz(self):
        """Date mode should format in user timezone."""
        from specivo.web.deps import _timeago

        # A datetime near midnight UTC — different date in Bangkok
        dt = datetime(2026, 4, 9, 23, 30, tzinfo=UTC)
        now = datetime(2026, 4, 10, 12, 0, tzinfo=UTC)
        result_bangkok = _timeago(dt, tz_name="Asia/Bangkok", mode="date", now=now)
        result_utc = _timeago(dt, tz_name="UTC", mode="date", now=now)
        # In Bangkok it's Apr 10, in UTC it's Apr 9
        assert "Apr 10" in result_bangkok
        assert result_utc == "Apr 9"


def _local(tz_name: str, day: int, hour: int, minute: int) -> datetime:
    """An aware datetime at the given wall-clock time in June 2026 in *tz_name*."""
    return datetime(2026, 6, day, hour, minute, tzinfo=ZoneInfo(tz_name))


@pytest.mark.unit
@pytest.mark.parametrize("tz_name", ["UTC", "Asia/Bangkok"])
class TestTimeagoMidnightBoundary:
    """Smart mode around local midnight, with "now" pinned to a local wall-clock time.

    Elapsed time is checked first: anything under 24 hours old reads as relative
    even across the date boundary, and "Yesterday" is reserved for items on the
    previous local calendar day that are at least 24 hours old.
    """

    def test_0001_item_from_two_minutes_before_midnight(self, tz_name):
        from specivo.web.deps import _timeago

        now = _local(tz_name, 10, 0, 1)
        dt = _local(tz_name, 9, 23, 58)
        assert _timeago(dt, tz_name=tz_name, now=now) == "3 min ago"

    def test_0001_item_from_previous_day_over_24h(self, tz_name):
        from specivo.web.deps import _timeago

        now = _local(tz_name, 10, 0, 1)
        dt = _local(tz_name, 9, 0, 0)
        assert _timeago(dt, tz_name=tz_name, now=now) == "Yesterday"

    def test_0001_item_from_two_days_ago_shows_date(self, tz_name):
        from specivo.web.deps import _timeago

        # Only 24h02m old, but two calendar days back: neither recent nor yesterday.
        now = _local(tz_name, 10, 0, 1)
        dt = _local(tz_name, 8, 23, 59)
        assert _timeago(dt, tz_name=tz_name, now=now) == "Jun 8"

    def test_0004_item_from_five_minutes_earlier(self, tz_name):
        from specivo.web.deps import _timeago

        now = _local(tz_name, 10, 0, 4)
        dt = now - timedelta(minutes=5)
        assert _timeago(dt, tz_name=tz_name, now=now) == "5 min ago"

    def test_0004_item_from_one_hour_earlier(self, tz_name):
        from specivo.web.deps import _timeago

        now = _local(tz_name, 10, 0, 4)
        dt = now - timedelta(hours=1)
        assert _timeago(dt, tz_name=tz_name, now=now) == "1 hr ago"

    def test_2359_item_from_same_morning(self, tz_name):
        from specivo.web.deps import _timeago

        now = _local(tz_name, 10, 23, 59)
        dt = _local(tz_name, 10, 0, 1)
        assert _timeago(dt, tz_name=tz_name, now=now) == "23 hrs ago"

    def test_2359_item_from_minutes_earlier(self, tz_name):
        from specivo.web.deps import _timeago

        now = _local(tz_name, 10, 23, 59)
        dt = now - timedelta(minutes=4)
        assert _timeago(dt, tz_name=tz_name, now=now) == "4 min ago"

    def test_2359_item_from_previous_evening(self, tz_name):
        from specivo.web.deps import _timeago

        now = _local(tz_name, 10, 23, 59)
        dt = _local(tz_name, 9, 23, 0)
        assert _timeago(dt, tz_name=tz_name, now=now) == "Yesterday"
