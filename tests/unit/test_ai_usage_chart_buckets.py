"""The time buckets and top groups of the My AI usage charts."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from specivo.services.ai_spend_report_service import MAX_PERIODS, coarsen_period
from specivo.services.ai_usage_chart_service import TOP_N, bucket_count, bucket_size, bucket_starts, top_groups

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("days", "size"),
    [(1, "day"), (62, "day"), (63, "week"), (371, "week"), (372, "month"), (3652, "month"), (3660, "quarter")],
)
def test_bucket_size_follows_the_range(days: int, size: str):
    first = date(2026, 1, 1)
    assert bucket_size(first, first + timedelta(days=days - 1)) == size


def test_bucket_size_goes_to_years_past_thirty_years():
    assert bucket_size(date(1990, 1, 1), date(2026, 3, 10)) == "year"


@pytest.mark.parametrize("chosen", ["day", "week", "month"])
def test_a_chosen_size_is_kept_only_within_the_cap(chosen: str):
    first = date(2026, 1, 1)
    assert bucket_size(first, first + timedelta(days=30), chosen) == chosen
    # Ten years: no chosen size fits in the cap, the automatic one is used.
    assert bucket_size(first, date(2035, 12, 31), chosen) == "month"
    assert bucket_size(first, first, "year") == "day"  # not a size one may choose


def test_bucket_starts_cover_the_range_evenly():
    assert bucket_starts(date(2026, 3, 30), date(2026, 4, 2), "day") == [
        date(2026, 3, 30),
        date(2026, 3, 31),
        date(2026, 4, 1),
        date(2026, 4, 2),
    ]
    # Weeks start on Monday: 2026-03-04 is a Wednesday.
    assert bucket_starts(date(2026, 3, 4), date(2026, 3, 20), "week") == [
        date(2026, 3, 2),
        date(2026, 3, 9),
        date(2026, 3, 16),
    ]
    assert bucket_starts(date(2025, 11, 15), date(2026, 4, 1), "quarter") == [
        date(2025, 10, 1),
        date(2026, 1, 1),
        date(2026, 4, 1),
    ]
    assert bucket_starts(date(2024, 6, 1), date(2026, 1, 1), "year") == [
        date(2024, 1, 1),
        date(2025, 1, 1),
        date(2026, 1, 1),
    ]
    assert bucket_starts(date(2025, 12, 15), date(2026, 2, 1), "month") == [
        date(2025, 12, 1),
        date(2026, 1, 1),
        date(2026, 2, 1),
    ]


def test_top_groups_keep_the_largest_and_never_an_other_of_one():
    totals = {f"m{i}": Decimal(i) for i in range(TOP_N + 1)}
    assert top_groups(totals) == [f"m{i}" for i in range(TOP_N, -1, -1)]
    totals["m-extra"] = Decimal(0)
    assert top_groups(totals) == [f"m{i}" for i in range(TOP_N, 0, -1)]


@pytest.mark.parametrize(
    ("first", "last"),
    [
        (date(2026, 3, 4), date(2026, 3, 20)),
        (date(2025, 12, 15), date(2026, 2, 1)),
        (date(2026, 1, 1), date(2026, 1, 1)),
        (date(2024, 2, 26), date(2026, 3, 1)),
    ],
)
@pytest.mark.parametrize("size", ["day", "week", "month", "quarter", "year"])
def test_bucket_count_matches_the_buckets(first: date, last: date, size: str):
    assert bucket_count(first, last, size) == len(bucket_starts(first, last, size))


def test_bucket_count_of_the_widest_range_builds_nothing():
    assert bucket_count(date.min, date.max, "month") == 9999 * 12
    assert bucket_count(date.min, date.max, "day") == (date.max - date.min).days + 1


def test_bucket_starts_never_step_past_date_max():
    assert bucket_starts(date(9999, 12, 30), date.max, "day") == [date(9999, 12, 30), date.max]
    assert bucket_starts(date(9999, 11, 20), date.max, "month") == [date(9999, 11, 1), date(9999, 12, 1)]
    assert bucket_starts(date(9998, 3, 1), date.max, "year") == [date(9998, 1, 1), date(9999, 1, 1)]
    assert bucket_starts(date(9999, 9, 1), date.max, "quarter") == [date(9999, 7, 1), date(9999, 10, 1)]
    assert bucket_starts(date(9999, 12, 20), date.max, "week")[-1] == date(9999, 12, 27)


def test_spend_report_periods_are_coarsened_past_the_cap():
    first = date(2024, 1, 1)
    assert coarsen_period(first, first + timedelta(days=MAX_PERIODS - 1), "day") == "day"
    assert coarsen_period(first, first + timedelta(days=MAX_PERIODS), "day") == "week"
    assert coarsen_period(first, first + timedelta(days=1099), "day") == "month"
    assert coarsen_period(first, first + timedelta(days=1099), "week") == "month"
    assert coarsen_period(first, first + timedelta(days=1099), "month") == "month"
    assert coarsen_period(first, first + timedelta(days=1099), None) is None
