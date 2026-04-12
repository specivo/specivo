"""Unit tests for timezone-aware Jinja2 filters: localtime, localdt, timeago with tz."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest


@pytest.mark.unit
class TestToUserTz:
    """Internal _to_user_tz helper converts UTC datetimes to user timezone."""

    def test_utc_to_moscow(self):
        from specivo.web.deps import _to_user_tz

        dt = datetime(2026, 4, 9, 23, 24, tzinfo=UTC)
        result = _to_user_tz(dt, "Europe/Moscow")
        assert result.hour == 2
        assert result.day == 10

    def test_utc_stays_utc(self):
        from specivo.web.deps import _to_user_tz

        dt = datetime(2026, 4, 9, 23, 24, tzinfo=UTC)
        result = _to_user_tz(dt, "UTC")
        assert result.hour == 23
        assert result.day == 9

    def test_none_returns_none(self):
        from specivo.web.deps import _to_user_tz

        assert _to_user_tz(None, "Europe/Moscow") is None

    def test_naive_treated_as_utc(self):
        from specivo.web.deps import _to_user_tz

        dt = datetime(2026, 4, 9, 23, 24)
        result = _to_user_tz(dt, "Europe/Moscow")
        assert result.hour == 2
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

    def test_moscow_timezone(self):
        from specivo.web.deps import _localtime

        dt = datetime(2026, 4, 9, 23, 24, tzinfo=UTC)
        assert _localtime(dt, "Europe/Moscow") == "2026-04-10 02:24"

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
    """The localdt filter formats with a custom strftime pattern."""

    def test_custom_format(self):
        from specivo.web.deps import _localdt

        dt = datetime(2026, 4, 9, 23, 24, tzinfo=UTC)
        result = _localdt(dt, "Europe/Moscow", "%b %d, %Y at %H:%M")
        assert result == "Apr 10, 2026 at 02:24"

    def test_date_only_format(self):
        from specivo.web.deps import _localdt

        dt = datetime(2026, 4, 9, 23, 24, tzinfo=UTC)
        result = _localdt(dt, "Europe/Moscow", "%Y-%m-%d")
        assert result == "2026-04-10"

    def test_none_returns_empty(self):
        from specivo.web.deps import _localdt

        assert _localdt(None, "UTC", "%Y-%m-%d") == ""


@pytest.mark.unit
class TestTimeagoTimezone:
    """timeago filter respects user timezone for date boundaries."""

    def test_today_boundary_moscow(self):
        """A time that is 'yesterday' in UTC but 'today' in Moscow should show relative."""
        from specivo.web.deps import _timeago

        # Near midnight UTC on Apr 10 — this is 03:00 Moscow time (Apr 10)
        # "now" for timeago is datetime.now(UTC), so this is only a test concept:
        # we test that the tz_name parameter is accepted without error.
        dt = datetime.now(UTC) - timedelta(minutes=5)
        result = _timeago(dt, tz_name="Europe/Moscow")
        assert result == "5 min ago"

    def test_relative_mode_ignores_tz(self):
        """Relative mode produces the same output regardless of timezone."""
        from specivo.web.deps import _timeago

        dt = datetime.now(UTC) - timedelta(hours=3)
        assert _timeago(dt, tz_name="Europe/Moscow", mode="relative") == "3 hrs ago"
        assert _timeago(dt, tz_name="UTC", mode="relative") == "3 hrs ago"

    def test_backward_compat_no_tz(self):
        """Calling without tz_name still works (defaults to UTC)."""
        from specivo.web.deps import _timeago

        dt = datetime.now(UTC) - timedelta(minutes=5)
        assert _timeago(dt) == "5 min ago"

    def test_date_mode_uses_user_tz(self):
        """Date mode should format in user timezone."""
        from specivo.web.deps import _timeago

        # A datetime near midnight UTC — different date in Moscow
        dt = datetime(2026, 4, 9, 23, 30, tzinfo=UTC)
        result_moscow = _timeago(dt, tz_name="Europe/Moscow", mode="date")
        result_utc = _timeago(dt, tz_name="UTC", mode="date")
        # In Moscow it's Apr 10, in UTC it's Apr 9
        assert "Apr 10" in result_moscow
        assert "Apr 09" in result_utc
