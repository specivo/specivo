"""Natural sort key and the shared version display order."""

from __future__ import annotations

import datetime
import random

import pytest

from specivo.core.natural_sort import natural_sort_key
from specivo.models.version import Version
from specivo.services.version_service import VERSION_SORT_OPTIONS, normalize_version_sort, order_versions

pytestmark = pytest.mark.unit


def _natural(names: list[str]) -> list[str]:
    return sorted(names, key=natural_sort_key)


# ---------------------------------------------------------------------------
# natural_sort_key
# ---------------------------------------------------------------------------


def test_digit_runs_compare_as_numbers():
    assert _natural(["0.11.0", "0.2.0", "0.10.0", "0.1.10", "0.1.9", "0.1.8"]) == [
        "0.1.8",
        "0.1.9",
        "0.1.10",
        "0.2.0",
        "0.10.0",
        "0.11.0",
    ]


def test_sprint_names():
    assert _natural(["Sprint 10", "Sprint 9", "Sprint 1", "Sprint 100"]) == [
        "Sprint 1",
        "Sprint 9",
        "Sprint 10",
        "Sprint 100",
    ]


def test_release_candidates():
    """A bare release sorts before its suffixed variants, and rc numbers compare numerically."""
    assert _natural(["v1.2.1", "v1.2-rc10", "v1.2-rc2", "v1.2", "v1.2-rc1", "v1.10"]) == [
        "v1.2",
        "v1.2-rc1",
        "v1.2-rc2",
        "v1.2-rc10",
        "v1.2.1",
        "v1.10",
    ]


def test_text_is_case_insensitive():
    assert _natural(["beta", "Alpha", "Gamma"]) == ["Alpha", "beta", "Gamma"]


def test_digits_and_text_never_compare_with_each_other():
    """Names starting with a digit and names starting with a letter sort without a TypeError."""
    assert _natural(["Backlog", "2.0", "10.0", "", "Q3 launch"]) == ["", "2.0", "10.0", "Backlog", "Q3 launch"]


def test_ties_fall_back_to_the_original_string():
    """Case-only and leading-zero differences still give a deterministic order."""
    names = ["v1", "V1", "1.01", "1.1"]
    assert _natural(names) == _natural(list(reversed(names)))
    assert natural_sort_key("1.01") != natural_sort_key("1.1")


def test_large_numbers():
    assert _natural(["build 99999999999999999999", "build 100000000000000000000"]) == [
        "build 99999999999999999999",
        "build 100000000000000000000",
    ]


# ---------------------------------------------------------------------------
# order_versions
# ---------------------------------------------------------------------------


def _v(name: str, status: str = "open", due: datetime.date | None = None) -> Version:
    return Version(name=name, status=status, effective_date=due)


_D = datetime.date


def _sample() -> list[Version]:
    versions = [
        _v("0.2.0", "closed", _D(2026, 3, 1)),
        _v("0.10.0", "closed"),
        _v("0.9.0", "closed", _D(2026, 8, 1)),
        _v("0.12.0", "open"),
        _v("0.11.0", "open", _D(2026, 11, 15)),
        _v("0.13.0", "locked", _D(2026, 11, 1)),
    ]
    random.Random(7).shuffle(versions)
    return versions


def _names(versions: list[Version]) -> list[str]:
    return [v.name for v in versions]


def test_default_order_is_active_by_due_then_closed():
    assert _names(order_versions(_sample())) == ["0.13.0", "0.11.0", "0.12.0", "0.2.0", "0.9.0", "0.10.0"]
    assert _names(order_versions(_sample(), "due:asc")) == _names(order_versions(_sample()))


@pytest.mark.parametrize(
    ("sort", "expected"),
    [
        ("version:asc", ["0.11.0", "0.12.0", "0.13.0", "0.2.0", "0.9.0", "0.10.0"]),
        ("version:desc", ["0.13.0", "0.12.0", "0.11.0", "0.10.0", "0.9.0", "0.2.0"]),
        ("due:desc", ["0.11.0", "0.13.0", "0.12.0", "0.9.0", "0.2.0", "0.10.0"]),
        ("status:asc", ["0.11.0", "0.12.0", "0.13.0", "0.2.0", "0.9.0", "0.10.0"]),
        ("status:desc", ["0.2.0", "0.9.0", "0.10.0", "0.13.0", "0.11.0", "0.12.0"]),
    ],
)
def test_sort_options(sort: str, expected: list[str]):
    assert _names(order_versions(_sample(), sort)) == expected


def test_invalid_sort_falls_back_to_default():
    assert normalize_version_sort("name; DROP TABLE") is None
    assert normalize_version_sort(None) is None
    assert _names(order_versions(_sample(), "bogus")) == _names(order_versions(_sample()))


def test_every_option_is_accepted():
    assert set(VERSION_SORT_OPTIONS) == {f"{f}:{d}" for f in ("version", "due", "status") for d in ("asc", "desc")}
    for option in VERSION_SORT_OPTIONS:
        assert normalize_version_sort(option) == option


def test_custom_attrs_accessor():
    rows = [("b", "open", None), ("a10", "open", None), ("a9", "open", None)]
    assert order_versions(rows, "version:asc", attrs=lambda r: r) == [
        ("a9", "open", None),
        ("a10", "open", None),
        ("b", "open", None),
    ]
