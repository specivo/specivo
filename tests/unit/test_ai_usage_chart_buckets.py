"""The time buckets and top groups of the My AI usage charts."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from specivo.services.ai_usage_chart_service import TOP_N, bucket_size, bucket_starts, top_groups

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("days", "size"),
    [(1, "day"), (60, "day"), (61, "week"), (731, "week"), (732, "month")],
)
def test_bucket_size_follows_the_range(days: int, size: str):
    first = date(2026, 1, 1)
    assert bucket_size(first, first + timedelta(days=days - 1)) == size


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
