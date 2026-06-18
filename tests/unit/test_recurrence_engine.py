"""Unit tests for the occurrence-expansion engine.

Pure logic — no DB, no fixtures, no async. Covers frequencies, intervals,
BY* parts, nth-weekday, BYSETPOS, DST correctness, irregular wall times,
working-day adjustment, COUNT/UNTIL/EXDATE termination, window bounding, the
rrule_raw path, and defensive validation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from specivo.services.recurrence.engine import (
    RecurrenceSpec,
    expand_occurrences,
    spec_from_mapping,
)

pytestmark = pytest.mark.unit

NY = ZoneInfo("America/New_York")
LONDON = ZoneInfo("Europe/London")


def _u(y, mo, d, h=0, mi=0):
    """Shorthand for a tz-aware UTC datetime."""
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


def _eod(y, mo, d):
    """End-of-day UTC bound, so a same-day 09:00 occurrence is inside the window."""
    return datetime(y, mo, d, 23, 59, tzinfo=UTC)


def _local_times(occ, tz):
    """Return list of 'HH:MM' local wall-clock strings for occurrences."""
    return [o.astimezone(tz).strftime("%H:%M") for o in occ]


def _dates(occ, tz=UTC):
    return [o.astimezone(tz).date().isoformat() for o in occ]


# ---------------------------------------------------------------------------
# Frequencies and intervals
# ---------------------------------------------------------------------------


class TestFrequencies:
    def test_daily(self):
        spec = RecurrenceSpec(freq="daily", dtstart=_u(2024, 1, 1, 9), timezone="UTC")
        occ = expand_occurrences(spec, _u(2024, 1, 1), _eod(2024, 1, 5))
        assert _dates(occ) == ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]

    def test_daily_every_3(self):
        spec = RecurrenceSpec(freq="daily", interval=3, dtstart=_u(2024, 1, 1, 9), timezone="UTC")
        occ = expand_occurrences(spec, _u(2024, 1, 1), _eod(2024, 1, 10))
        assert _dates(occ) == ["2024-01-01", "2024-01-04", "2024-01-07", "2024-01-10"]

    def test_weekly(self):
        # 2024-01-01 is a Monday.
        spec = RecurrenceSpec(freq="weekly", dtstart=_u(2024, 1, 1, 9), timezone="UTC")
        occ = expand_occurrences(spec, _u(2024, 1, 1), _u(2024, 1, 31))
        assert _dates(occ) == ["2024-01-01", "2024-01-08", "2024-01-15", "2024-01-22", "2024-01-29"]

    def test_weekly_every_2(self):
        spec = RecurrenceSpec(freq="weekly", interval=2, dtstart=_u(2024, 1, 1, 9), timezone="UTC")
        occ = expand_occurrences(spec, _u(2024, 1, 1), _u(2024, 2, 29))
        assert _dates(occ) == ["2024-01-01", "2024-01-15", "2024-01-29", "2024-02-12", "2024-02-26"]

    def test_monthly(self):
        spec = RecurrenceSpec(freq="monthly", dtstart=_u(2024, 1, 15, 9), timezone="UTC")
        occ = expand_occurrences(spec, _u(2024, 1, 1), _u(2024, 4, 30))
        assert _dates(occ) == ["2024-01-15", "2024-02-15", "2024-03-15", "2024-04-15"]

    def test_monthly_every_3(self):
        spec = RecurrenceSpec(freq="monthly", interval=3, dtstart=_u(2024, 1, 10, 9), timezone="UTC")
        occ = expand_occurrences(spec, _u(2024, 1, 1), _u(2024, 12, 31))
        assert _dates(occ) == ["2024-01-10", "2024-04-10", "2024-07-10", "2024-10-10"]

    def test_yearly(self):
        spec = RecurrenceSpec(freq="yearly", dtstart=_u(2024, 6, 1, 9), timezone="UTC")
        occ = expand_occurrences(spec, _u(2024, 1, 1), _u(2027, 1, 1))
        assert _dates(occ) == ["2024-06-01", "2025-06-01", "2026-06-01"]

    def test_yearly_every_2(self):
        spec = RecurrenceSpec(freq="yearly", interval=2, dtstart=_u(2020, 2, 29, 9), timezone="UTC")
        occ = expand_occurrences(spec, _u(2020, 1, 1), _u(2030, 1, 1))
        # Feb 29 only exists in leap years; dateutil keeps only valid Feb-29 years.
        assert _dates(occ) == ["2020-02-29", "2024-02-29", "2028-02-29"]


# ---------------------------------------------------------------------------
# BYDAY lists and nth-weekday
# ---------------------------------------------------------------------------


class TestByday:
    def test_byday_list_mwf(self):
        spec = RecurrenceSpec(
            freq="weekly",
            byday=["MO", "WE", "FR"],
            dtstart=_u(2024, 1, 1, 9),  # Monday
            timezone="UTC",
        )
        occ = expand_occurrences(spec, _u(2024, 1, 1), _u(2024, 1, 7))
        assert _dates(occ) == ["2024-01-01", "2024-01-03", "2024-01-05"]

    def test_first_monday_of_month(self):
        spec = RecurrenceSpec(
            freq="monthly",
            byday=["1MO"],
            dtstart=_u(2024, 1, 1, 9),
            timezone="UTC",
        )
        occ = expand_occurrences(spec, _u(2024, 1, 1), _u(2024, 4, 30))
        # 1st Mondays: Jan 1, Feb 5, Mar 4, Apr 1.
        assert _dates(occ) == ["2024-01-01", "2024-02-05", "2024-03-04", "2024-04-01"]

    def test_last_friday_of_month(self):
        spec = RecurrenceSpec(
            freq="monthly",
            byday=["-1FR"],
            dtstart=_u(2024, 1, 1, 9),
            timezone="UTC",
        )
        occ = expand_occurrences(spec, _u(2024, 1, 1), _u(2024, 3, 31))
        # Last Fridays: Jan 26, Feb 23, Mar 29.
        assert _dates(occ) == ["2024-01-26", "2024-02-23", "2024-03-29"]

    def test_last_weekday_via_negative_ordinal(self):
        # Last Sunday of each month.
        spec = RecurrenceSpec(
            freq="monthly",
            byday=["-1SU"],
            dtstart=_u(2024, 1, 1, 9),
            timezone="UTC",
        )
        occ = expand_occurrences(spec, _u(2024, 1, 1), _eod(2024, 3, 31))
        assert _dates(occ) == ["2024-01-28", "2024-02-25", "2024-03-31"]

    def test_plus_prefixed_ordinal(self):
        spec = RecurrenceSpec(
            freq="monthly",
            byday=["+2TU"],
            dtstart=_u(2024, 1, 1, 9),
            timezone="UTC",
        )
        occ = expand_occurrences(spec, _u(2024, 1, 1), _u(2024, 2, 29))
        # 2nd Tuesdays: Jan 9, Feb 13.
        assert _dates(occ) == ["2024-01-09", "2024-02-13"]


# ---------------------------------------------------------------------------
# BYSETPOS and BYMONTHDAY
# ---------------------------------------------------------------------------


class TestBysetposAndBymonthday:
    def test_second_tuesday_via_bysetpos(self):
        spec = RecurrenceSpec(
            freq="monthly",
            byday=["TU"],
            bysetpos=[2],
            dtstart=_u(2024, 1, 1, 9),
            timezone="UTC",
        )
        occ = expand_occurrences(spec, _u(2024, 1, 1), _u(2024, 3, 31))
        # 2nd Tuesdays: Jan 9, Feb 13, Mar 12.
        assert _dates(occ) == ["2024-01-09", "2024-02-13", "2024-03-12"]

    def test_last_weekday_of_month_via_bysetpos(self):
        # Last working weekday of the month: weekdays MO-FR, bysetpos -1.
        spec = RecurrenceSpec(
            freq="monthly",
            byday=["MO", "TU", "WE", "TH", "FR"],
            bysetpos=[-1],
            dtstart=_u(2024, 1, 1, 9),
            timezone="UTC",
        )
        occ = expand_occurrences(spec, _u(2024, 1, 1), _u(2024, 3, 31))
        # Last weekday: Jan 31 (Wed), Feb 29 (Thu), Mar 29 (Fri).
        assert _dates(occ) == ["2024-01-31", "2024-02-29", "2024-03-29"]

    def test_bymonthday_list(self):
        spec = RecurrenceSpec(
            freq="monthly",
            bymonthday=[1, 15],
            dtstart=_u(2024, 1, 1, 9),
            timezone="UTC",
        )
        occ = expand_occurrences(spec, _u(2024, 1, 1), _u(2024, 2, 29))
        assert _dates(occ) == ["2024-01-01", "2024-01-15", "2024-02-01", "2024-02-15"]

    def test_bymonthday_last_day(self):
        spec = RecurrenceSpec(
            freq="monthly",
            bymonthday=[-1],
            dtstart=_u(2024, 1, 1, 9),
            timezone="UTC",
        )
        occ = expand_occurrences(spec, _u(2024, 1, 1), _eod(2024, 4, 30))
        # Last days, incl. leap-year Feb 29.
        assert _dates(occ) == ["2024-01-31", "2024-02-29", "2024-03-31", "2024-04-30"]

    def test_bymonth_filter(self):
        spec = RecurrenceSpec(
            freq="yearly",
            bymonth=[3, 6],
            bymonthday=[15],
            dtstart=_u(2024, 1, 1, 9),
            timezone="UTC",
        )
        occ = expand_occurrences(spec, _u(2024, 1, 1), _u(2025, 12, 31))
        assert _dates(occ) == ["2024-03-15", "2024-06-15", "2025-03-15", "2025-06-15"]


# ---------------------------------------------------------------------------
# DST correctness
# ---------------------------------------------------------------------------


class TestDST:
    def test_daily_0900_across_us_spring_forward(self):
        # US spring-forward: 2024-03-10 (2nd Sunday March).
        spec = RecurrenceSpec(freq="daily", dtstart=datetime(2024, 3, 8, 9, 0, tzinfo=NY), timezone="America/New_York")
        occ = expand_occurrences(spec, _u(2024, 3, 8), _u(2024, 3, 13))
        # Local time stays 09:00 throughout.
        assert _local_times(occ, NY) == ["09:00"] * len(occ)
        # UTC offset shifts -05:00 -> -04:00 at the boundary: 14:00Z before, 13:00Z after.
        utc_hours = [o.hour for o in occ]
        assert utc_hours[0] == 14  # 2024-03-08, EST
        assert utc_hours[-1] == 13  # after DST, EDT

    def test_daily_0900_across_us_fall_back(self):
        # US fall-back: 2024-11-03 (1st Sunday Nov).
        spec = RecurrenceSpec(freq="daily", dtstart=datetime(2024, 11, 1, 9, 0, tzinfo=NY), timezone="America/New_York")
        occ = expand_occurrences(spec, _u(2024, 11, 1), _u(2024, 11, 6))
        assert _local_times(occ, NY) == ["09:00"] * len(occ)
        utc_hours = [o.hour for o in occ]
        assert utc_hours[0] == 13  # EDT
        assert utc_hours[-1] == 14  # EST after fall-back

    def test_daily_0900_across_eu_spring_forward(self):
        # EU/London spring-forward: 2024-03-31.
        spec = RecurrenceSpec(
            freq="daily",
            dtstart=datetime(2024, 3, 29, 9, 0, tzinfo=LONDON),
            timezone="Europe/London",
        )
        occ = expand_occurrences(spec, _u(2024, 3, 29), _u(2024, 4, 3))
        assert _local_times(occ, LONDON) == ["09:00"] * len(occ)
        utc_hours = [o.hour for o in occ]
        assert utc_hours[0] == 9  # GMT (UTC+0)
        assert utc_hours[-1] == 8  # BST (UTC+1)

    def test_weekly_meeting_time_fixed_across_transition(self):
        # Weekly Monday 14:00 NY meeting spanning spring-forward.
        spec = RecurrenceSpec(
            freq="weekly",
            byday=["MO"],
            dtstart=datetime(2024, 3, 4, 14, 0, tzinfo=NY),
            timezone="America/New_York",
        )
        occ = expand_occurrences(spec, _u(2024, 3, 4), _eod(2024, 3, 25))
        assert _local_times(occ, NY) == ["14:00", "14:00", "14:00", "14:00"]
        # 2024-03-04 EST -> 19:00Z; 03-11, 03-18, 03-25 EDT -> 18:00Z.
        assert [o.hour for o in occ] == [19, 18, 18, 18]


# ---------------------------------------------------------------------------
# Irregular wall times
# ---------------------------------------------------------------------------


class TestIrregularWallTimes:
    def test_nonexistent_time_shifts_forward(self):
        # 02:30 does not exist on US spring-forward day (gap 02:00->03:00).
        spec = RecurrenceSpec(
            freq="daily",
            dtstart=datetime(2024, 3, 9, 2, 30, tzinfo=NY),
            timezone="America/New_York",
        )
        occ = expand_occurrences(spec, _u(2024, 3, 9), _u(2024, 3, 12))
        local = [o.astimezone(NY).strftime("%m-%d %H:%M") for o in occ]
        # 03-10 02:30 is in the gap -> shifted forward to the first valid 03:30.
        assert local[0] == "03-09 02:30"
        assert local[1] == "03-10 03:00"
        assert local[2] == "03-11 02:30"

    def test_ambiguous_time_picks_fold_zero_earlier(self):
        # 01:30 occurs twice on US fall-back day (2024-11-03). Policy: fold=0 (earlier).
        spec = RecurrenceSpec(
            freq="daily",
            dtstart=datetime(2024, 11, 2, 1, 30, tzinfo=NY),
            timezone="America/New_York",
        )
        occ = expand_occurrences(spec, _u(2024, 11, 2), _u(2024, 11, 5))
        # Find the 11-03 occurrence and assert it is the earlier (EDT, fold=0) instant.
        fallback = [o for o in occ if o.astimezone(NY).date().isoformat() == "2024-11-03"][0]
        # fold=0 earlier instant is 01:30 EDT = 05:30 UTC (EDT = UTC-4).
        assert fallback == _u(2024, 11, 3, 5, 30)
        # Local wall clock still reads 01:30.
        assert fallback.astimezone(NY).strftime("%H:%M") == "01:30"


# ---------------------------------------------------------------------------
# Working-day adjustment
# ---------------------------------------------------------------------------


class TestWorkingDayAdjustment:
    def test_weekend_next(self):
        # 2024-01-06 is a Saturday; next working day is Monday 2024-01-08.
        spec = RecurrenceSpec(
            freq="monthly",
            bymonthday=[6],
            dtstart=_u(2024, 1, 6, 9),
            timezone="UTC",
            working_day_adjustment="next",
        )
        occ = expand_occurrences(spec, _u(2024, 1, 1), _u(2024, 1, 31))
        assert _dates(occ) == ["2024-01-08"]
        assert _local_times(occ, UTC) == ["09:00"]  # time-of-day preserved

    def test_weekend_previous(self):
        spec = RecurrenceSpec(
            freq="monthly",
            bymonthday=[6],
            dtstart=_u(2024, 1, 6, 9),
            timezone="UTC",
            working_day_adjustment="previous",
        )
        occ = expand_occurrences(spec, _u(2024, 1, 1), _u(2024, 1, 31))
        # Saturday -> previous working day = Friday 2024-01-05.
        assert _dates(occ) == ["2024-01-05"]

    def test_weekend_nearest_sunday_to_monday(self):
        # 2024-01-07 is a Sunday; nearest working day is Monday (1 day fwd vs 2 back).
        spec = RecurrenceSpec(
            freq="monthly",
            bymonthday=[7],
            dtstart=_u(2024, 1, 7, 9),
            timezone="UTC",
            working_day_adjustment="nearest",
        )
        occ = expand_occurrences(spec, _u(2024, 1, 1), _u(2024, 1, 31))
        assert _dates(occ) == ["2024-01-08"]

    def test_weekend_nearest_saturday_to_friday(self):
        # 2024-01-06 Saturday; nearest = Friday (1 back) beats Monday (2 fwd).
        spec = RecurrenceSpec(
            freq="monthly",
            bymonthday=[6],
            dtstart=_u(2024, 1, 6, 9),
            timezone="UTC",
            working_day_adjustment="nearest",
        )
        occ = expand_occurrences(spec, _u(2024, 1, 1), _u(2024, 1, 31))
        assert _dates(occ) == ["2024-01-05"]

    def test_nearest_tie_goes_to_next(self):
        # Construct a tie: working days Mon-Fri but make Friday a holiday and
        # Monday a holiday so a Saturday is equidistant (Thu back 2, Tue fwd 3)?
        # Simpler explicit tie: a non-working Wednesday with Tue and Thu working
        # is not a tie. Use a single working day to force symmetric distance.
        # Working days = {Wed(3)} only; an occurrence on Wed is fine, but on
        # the surrounding Sat... Instead: working days Tue & Thu, occurrence Wed
        # -> Tue (1 back) vs Thu (1 fwd): tie -> next (Thu).
        spec = RecurrenceSpec(
            freq="weekly",
            byday=["WE"],
            dtstart=_u(2024, 1, 3, 9),  # Wednesday
            timezone="UTC",
            working_day_adjustment="nearest",
            working_days=[2, 4],  # Tue, Thu
        )
        occ = expand_occurrences(spec, _u(2024, 1, 1), _eod(2024, 1, 4))
        # Wed 2024-01-03 -> tie between Tue 01-02 and Thu 01-04 -> next (Thu).
        assert _dates(occ) == ["2024-01-04"]

    def test_holiday_in_calendar_shifts(self):
        # 2024-01-01 (Mon) is a working day but listed as a holiday -> next.
        spec = RecurrenceSpec(
            freq="monthly",
            bymonthday=[1],
            dtstart=_u(2024, 1, 1, 9),
            timezone="UTC",
            working_day_adjustment="next",
            holiday_calendar=["2024-01-01"],
        )
        occ = expand_occurrences(spec, _u(2024, 1, 1), _u(2024, 1, 31))
        assert _dates(occ) == ["2024-01-02"]

    def test_consecutive_non_working_days_skipped(self):
        # Occurrence on Saturday with Sat+Sun weekend AND Monday a holiday ->
        # next working day is Tuesday.
        spec = RecurrenceSpec(
            freq="monthly",
            bymonthday=[6],  # 2024-01-06 Saturday
            dtstart=_u(2024, 1, 6, 9),
            timezone="UTC",
            working_day_adjustment="next",
            holiday_calendar=["2024-01-08"],  # Monday holiday
        )
        occ = expand_occurrences(spec, _u(2024, 1, 1), _u(2024, 1, 31))
        # Sat -> Sun (off) -> Mon (holiday) -> Tue 2024-01-09.
        assert _dates(occ) == ["2024-01-09"]

    def test_collision_dedupe(self):
        # Two occurrences on consecutive weekend days both shift onto the same
        # Monday; the duplicate is dropped deterministically.
        # bymonthday 6 (Sat) and 7 (Sun) both -> Monday 2024-01-08 with 'next'.
        spec = RecurrenceSpec(
            freq="monthly",
            bymonthday=[6, 7],
            dtstart=_u(2024, 1, 6, 9),
            timezone="UTC",
            working_day_adjustment="next",
        )
        occ = expand_occurrences(spec, _u(2024, 1, 1), _u(2024, 1, 31))
        # Both collapse to 2024-01-08 09:00; only one survives.
        assert _dates(occ) == ["2024-01-08"]
        assert len(occ) == 1

    def test_no_adjustment_leaves_weekend(self):
        spec = RecurrenceSpec(
            freq="monthly",
            bymonthday=[6],  # Saturday
            dtstart=_u(2024, 1, 6, 9),
            timezone="UTC",
            working_day_adjustment="none",
        )
        occ = expand_occurrences(spec, _u(2024, 1, 1), _u(2024, 1, 31))
        assert _dates(occ) == ["2024-01-06"]


# ---------------------------------------------------------------------------
# Termination: COUNT, UNTIL, EXDATE
# ---------------------------------------------------------------------------


class TestTermination:
    def test_count(self):
        spec = RecurrenceSpec(freq="daily", dtstart=_u(2024, 1, 1, 9), timezone="UTC", count=3)
        occ = expand_occurrences(spec, _u(2024, 1, 1), _u(2024, 12, 31))
        assert _dates(occ) == ["2024-01-01", "2024-01-02", "2024-01-03"]

    def test_until(self):
        spec = RecurrenceSpec(
            freq="daily",
            dtstart=_u(2024, 1, 1, 9),
            timezone="UTC",
            until=_u(2024, 1, 3, 9),
        )
        occ = expand_occurrences(spec, _u(2024, 1, 1), _u(2024, 12, 31))
        # UNTIL is inclusive of the matching instant.
        assert _dates(occ) == ["2024-01-01", "2024-01-02", "2024-01-03"]

    def test_exdate_does_not_consume_count(self):
        spec = RecurrenceSpec(freq="daily", dtstart=_u(2024, 1, 1, 9), timezone="UTC", count=5)
        exdates = [_u(2024, 1, 3, 9)]
        occ = expand_occurrences(spec, _u(2024, 1, 1), _u(2024, 12, 31), exdates=exdates)
        # 5 live occurrences: the skipped 01-03 is replaced by continuing to 01-06.
        assert len(occ) == 5
        assert _dates(occ) == ["2024-01-01", "2024-01-02", "2024-01-04", "2024-01-05", "2024-01-06"]

    def test_multiple_exdates_do_not_consume_count(self):
        spec = RecurrenceSpec(freq="daily", dtstart=_u(2024, 1, 1, 9), timezone="UTC", count=5)
        exdates = [_u(2024, 1, 2, 9), _u(2024, 1, 4, 9)]
        occ = expand_occurrences(spec, _u(2024, 1, 1), _u(2024, 12, 31), exdates=exdates)
        assert len(occ) == 5
        assert _dates(occ) == ["2024-01-01", "2024-01-03", "2024-01-05", "2024-01-06", "2024-01-07"]

    def test_exdate_removes_occurrence_no_count(self):
        # Without COUNT, an exdate simply removes that occurrence.
        spec = RecurrenceSpec(freq="daily", dtstart=_u(2024, 1, 1, 9), timezone="UTC")
        exdates = [_u(2024, 1, 2, 9)]
        occ = expand_occurrences(spec, _u(2024, 1, 1), _eod(2024, 1, 4), exdates=exdates)
        assert _dates(occ) == ["2024-01-01", "2024-01-03", "2024-01-04"]


# ---------------------------------------------------------------------------
# Window bounding
# ---------------------------------------------------------------------------


class TestWindowBounding:
    def test_excludes_before_window_start(self):
        spec = RecurrenceSpec(freq="daily", dtstart=_u(2024, 1, 1, 9), timezone="UTC")
        occ = expand_occurrences(spec, _u(2024, 1, 3), _eod(2024, 1, 5))
        assert _dates(occ) == ["2024-01-03", "2024-01-04", "2024-01-05"]

    def test_excludes_after_window_end(self):
        spec = RecurrenceSpec(freq="daily", dtstart=_u(2024, 1, 1, 9), timezone="UTC")
        occ = expand_occurrences(spec, _u(2024, 1, 1), _u(2024, 1, 3, 12))
        # window_end 01-03 12:00 > 01-03 09:00, so 01-03 included; 01-04 excluded.
        assert _dates(occ) == ["2024-01-01", "2024-01-02", "2024-01-03"]

    def test_window_start_inclusive(self):
        spec = RecurrenceSpec(freq="daily", dtstart=_u(2024, 1, 1, 9), timezone="UTC")
        occ = expand_occurrences(spec, _u(2024, 1, 1, 9), _u(2024, 1, 2))
        # Exactly window_start matches the first occurrence -> included.
        assert _dates(occ) == ["2024-01-01"]

    def test_window_end_inclusive(self):
        spec = RecurrenceSpec(freq="daily", dtstart=_u(2024, 1, 1, 9), timezone="UTC")
        occ = expand_occurrences(spec, _u(2024, 1, 1), _u(2024, 1, 2, 9))
        # window_end exactly equals 01-02 09:00 -> included.
        assert _dates(occ) == ["2024-01-01", "2024-01-02"]

    def test_empty_when_window_before_series(self):
        spec = RecurrenceSpec(freq="daily", dtstart=_u(2024, 6, 1, 9), timezone="UTC")
        occ = expand_occurrences(spec, _u(2024, 1, 1), _u(2024, 1, 31))
        assert occ == []


# ---------------------------------------------------------------------------
# rrule_raw path
# ---------------------------------------------------------------------------


class TestRruleRaw:
    def test_raw_matches_discrete_fields(self):
        discrete = RecurrenceSpec(
            freq="weekly",
            byday=["MO", "WE", "FR"],
            dtstart=_u(2024, 1, 1, 9),
            timezone="UTC",
        )
        raw = RecurrenceSpec(
            freq="weekly",  # ignored when rrule_raw set
            dtstart=_u(2024, 1, 1, 9),
            timezone="UTC",
            rrule_raw="FREQ=WEEKLY;BYDAY=MO,WE,FR",
        )
        ws, we = _u(2024, 1, 1), _u(2024, 1, 14)
        assert expand_occurrences(discrete, ws, we) == expand_occurrences(raw, ws, we)

    def test_raw_with_count(self):
        raw = RecurrenceSpec(
            freq="daily",
            dtstart=_u(2024, 1, 1, 9),
            timezone="UTC",
            rrule_raw="FREQ=DAILY;COUNT=3",
        )
        occ = expand_occurrences(raw, _u(2024, 1, 1), _u(2024, 12, 31))
        assert _dates(occ) == ["2024-01-01", "2024-01-02", "2024-01-03"]

    def test_raw_dst_correctness(self):
        # Raw rule still expands in local wall-clock space.
        raw = RecurrenceSpec(
            freq="daily",
            dtstart=datetime(2024, 3, 8, 9, 0, tzinfo=NY),
            timezone="America/New_York",
            rrule_raw="FREQ=DAILY",
        )
        occ = expand_occurrences(raw, _u(2024, 3, 8), _u(2024, 3, 13))
        assert _local_times(occ, NY) == ["09:00"] * len(occ)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_count_and_until_mutually_exclusive(self):
        spec = RecurrenceSpec(
            freq="daily",
            dtstart=_u(2024, 1, 1, 9),
            timezone="UTC",
            count=3,
            until=_u(2024, 6, 1),
        )
        with pytest.raises(ValueError, match="mutually exclusive"):
            expand_occurrences(spec, _u(2024, 1, 1), _u(2024, 12, 31))

    def test_bysetpos_without_byparts_raises(self):
        spec = RecurrenceSpec(
            freq="monthly",
            bysetpos=[2],
            dtstart=_u(2024, 1, 1, 9),
            timezone="UTC",
        )
        with pytest.raises(ValueError, match="bysetpos requires"):
            expand_occurrences(spec, _u(2024, 1, 1), _u(2024, 12, 31))

    def test_invalid_freq_raises(self):
        spec = RecurrenceSpec(freq="hourly", dtstart=_u(2024, 1, 1, 9), timezone="UTC")
        with pytest.raises(ValueError, match="invalid freq"):
            expand_occurrences(spec, _u(2024, 1, 1), _u(2024, 12, 31))

    def test_zero_interval_raises(self):
        spec = RecurrenceSpec(freq="daily", interval=0, dtstart=_u(2024, 1, 1, 9), timezone="UTC")
        with pytest.raises(ValueError, match="interval must be"):
            expand_occurrences(spec, _u(2024, 1, 1), _u(2024, 12, 31))

    def test_naive_dtstart_raises(self):
        spec = RecurrenceSpec(freq="daily", dtstart=datetime(2024, 1, 1, 9), timezone="UTC")
        with pytest.raises(ValueError, match="timezone-aware"):
            expand_occurrences(spec, _u(2024, 1, 1), _u(2024, 12, 31))

    def test_invalid_byday_code_raises(self):
        spec = RecurrenceSpec(freq="weekly", byday=["XX"], dtstart=_u(2024, 1, 1, 9), timezone="UTC")
        with pytest.raises(ValueError, match="invalid byday"):
            expand_occurrences(spec, _u(2024, 1, 1), _u(2024, 12, 31))

    def test_naive_window_raises(self):
        spec = RecurrenceSpec(freq="daily", dtstart=_u(2024, 1, 1, 9), timezone="UTC")
        with pytest.raises(ValueError, match="window bounds must be"):
            expand_occurrences(spec, datetime(2024, 1, 1), _u(2024, 12, 31))


# ---------------------------------------------------------------------------
# spec_from_mapping helper
# ---------------------------------------------------------------------------


class TestSpecFromMapping:
    def test_basic_mapping(self):
        spec = spec_from_mapping(
            {
                "freq": "daily",
                "dtstart": _u(2024, 1, 1, 9),
                "timezone": "UTC",
                "rrule_interval": 2,
            }
        )
        assert spec.freq == "daily"
        assert spec.interval == 2
        occ = expand_occurrences(spec, _u(2024, 1, 1), _eod(2024, 1, 5))
        assert _dates(occ) == ["2024-01-01", "2024-01-03", "2024-01-05"]

    def test_orm_alias_keys(self):
        spec = spec_from_mapping(
            {
                "freq": "daily",
                "dtstart": _u(2024, 1, 1, 9),
                "rrule_count": 4,
            }
        )
        assert spec.count == 4
        assert spec.interval == 1
        assert spec.working_days == [1, 2, 3, 4, 5]

    def test_mapping_defaults(self):
        spec = spec_from_mapping({"freq": "weekly", "dtstart": _u(2024, 1, 1, 9)})
        assert spec.timezone == "UTC"
        assert spec.working_day_adjustment == "none"
        assert spec.holiday_calendar is None
