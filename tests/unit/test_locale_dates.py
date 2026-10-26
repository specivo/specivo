"""Server-rendered dates and relative times follow the viewer's language.

The ``timeago``, ``localdt`` and ``localdate`` filters used strftime month
names and English relative-time phrases, so a Thai page still read
"Apr 10" and "3 hrs ago". They now go through Babel's CLDR data and the
gettext catalogues for the active locale.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import pytest

from specivo.core.i18n import activate, deactivate, format_date, format_datetime

pytestmark = pytest.mark.unit

_APR_10_UTC = datetime(2026, 4, 9, 23, 24, tzinfo=UTC)  # 06:24 on Apr 10 in Bangkok


@pytest.fixture
def thai() -> Iterator[None]:
    activate("th")
    try:
        yield
    finally:
        deactivate()


# ---------------------------------------------------------------------------
# specivo.core.i18n helpers
# ---------------------------------------------------------------------------


def test_format_date_uses_the_active_locale(thai):
    assert format_date(date(2026, 4, 10), "MMMd") == "10 เม.ย."
    assert format_date(date(2026, 4, 10)) == "10 เม.ย. 2026"


def test_format_date_english_by_default():
    assert format_date(date(2026, 4, 10), "MMMd") == "Apr 10"
    assert format_date(date(2026, 4, 10)) == "Apr 10, 2026"


def test_format_date_keeps_the_gregorian_year(thai):
    """Babel renders Thai dates in the Gregorian calendar; the browser side forces the same."""
    assert "2026" in format_date(date(2026, 4, 10))


def test_format_datetime_in_thai(thai):
    assert format_datetime(datetime(2026, 4, 10, 6, 24)) == "10 เม.ย. 2026 06:24 น."


def test_unknown_locale_falls_back_to_english():
    assert format_date(date(2026, 4, 10), "MMMd", locale="not-a-locale") == "Apr 10"
    assert format_datetime(datetime(2026, 4, 10, 6, 24), locale="not-a-locale") == "Apr 10, 2026, 06:24"


# ---------------------------------------------------------------------------
# Jinja filters
# ---------------------------------------------------------------------------


def test_localdt_in_thai_uses_the_user_timezone(thai):
    from specivo.web.deps import _localdt

    assert _localdt(_APR_10_UTC, "Asia/Bangkok") == "10 เม.ย. 2026 06:24 น."
    assert _localdt(_APR_10_UTC, "Asia/Bangkok", "yMMMd") == "10 เม.ย. 2026"


def test_localdate_in_thai(thai):
    from specivo.web.deps import _localdate

    assert _localdate(date(2026, 4, 10), "MMMd") == "10 เม.ย."
    assert _localdate(None) == ""


def test_timeago_date_mode_in_thai(thai):
    from specivo.web.deps import _timeago

    assert _timeago(_APR_10_UTC, tz_name="Asia/Bangkok", mode="date") in {"10 เม.ย.", "10 เม.ย. 2026"}


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (timedelta(seconds=5), "เมื่อสักครู่"),
        (timedelta(minutes=5), "5 นาทีที่แล้ว"),
        (timedelta(hours=3), "3 ชม. ที่แล้ว"),
        (timedelta(days=4), "4 วันที่แล้ว"),
        (timedelta(days=14), "2 สัปดาห์ที่แล้ว"),
        (timedelta(days=90), "3 เดือนที่แล้ว"),
        (timedelta(days=800), "2 ปีที่แล้ว"),
    ],
)
def test_timeago_relative_phrases_in_thai(thai, delta: timedelta, expected: str):
    from specivo.web.deps import _timeago

    assert _timeago(datetime.now(UTC) - delta, mode="relative") == expected


def test_timeago_yesterday_in_thai(thai):
    from specivo.web.deps import _timeago

    assert _timeago(datetime.now(UTC) - timedelta(days=1)) == "เมื่อวาน"


@pytest.mark.parametrize("locale", ["es", "fr", "ru", "th", "zh"])
def test_timeago_is_translated_in_every_shipped_locale(locale: str):
    from specivo.web.deps import _timeago

    activate(locale)
    try:
        for delta in (timedelta(hours=1), timedelta(hours=5), timedelta(days=21)):
            text = _timeago(datetime.now(UTC) - delta, mode="relative")
            assert "ago" not in text, f"{locale!r} left {text!r} in English"
    finally:
        deactivate()
