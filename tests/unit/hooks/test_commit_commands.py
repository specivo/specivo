"""Commit commands in messages: time after a reference, and the fixing keyword (``specivo.hooks.refs``)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from specivo.hooks.refs import MAX_LOGGED_HOURS, extract_refs, extract_time

pytestmark = pytest.mark.unit

KEYS = frozenset({"ACME"})


@pytest.mark.parametrize(
    ("text", "hours"),
    [
        (" @2h", Decimal("2")),
        ("@2h", Decimal("2")),
        (" @1h30m", Decimal("1.5")),
        (" @90m", Decimal("1.5")),
        (" @1.5h", Decimal("1.5")),
        (" @0h30m", Decimal("0.5")),
        (" @45m", Decimal("0.75")),
        (" @10m", Decimal("0.17")),
        (" @2h tidy the parser", Decimal("2")),
        (" @2h, then more", Decimal("2")),
        (" @2H", Decimal("2")),
        ("\t@3h\n", Decimal("3")),
        (" @24h", Decimal("24")),
        (" @25h", MAX_LOGGED_HOURS),
        (" @100h", MAX_LOGGED_HOURS),
    ],
)
def test_extract_time(text: str, hours: Decimal) -> None:
    assert extract_time(text) == hours


@pytest.mark.parametrize(
    "text",
    [
        "",
        " no time",
        " @0h",
        " @0m",
        " @0h0m",
        " @2",
        " @h",
        " @2x",
        " @2hours",
        " @1.5h30m",
        " @1,5h",
        " @.5h",
        " @2h@3h",
        "@2host",
        " user@2h",
        " see @2h later",
        "\n\n@2h on another paragraph",
        " @" + "9" * 5000 + "h",
    ],
)
def test_extract_time_rejects(text: str) -> None:
    assert extract_time(text) is None


def test_time_follows_its_reference() -> None:
    refs = {r.display_key: r for r in extract_refs("Fixes ACME-1 @1h30m, refs ACME-2 @45m and ACME-3", KEYS)}
    assert refs["ACME-1"].hours == Decimal("1.5")
    assert refs["ACME-1"].keyword == "fixes"
    assert refs["ACME-2"].hours == Decimal("0.75")
    assert refs["ACME-3"].hours is None


def test_time_in_an_email_address_is_not_logged() -> None:
    (ref,) = extract_refs("ACME-1 mail user@2host about it", KEYS)
    assert ref.hours is None


def test_first_time_for_an_issue_wins() -> None:
    (ref,) = extract_refs("ACME-1 @1h and again ACME-1 @2h", KEYS)
    assert ref.hours == Decimal("1")


@pytest.mark.parametrize(
    ("message", "keyword"),
    [
        ("Fixes ACME-1", "fixes"),
        ("closes ACME-1", "fixes"),
        ("Resolved ACME-1", "fixes"),
        ("refs ACME-1", "refs"),
        ("ACME-1", "refs"),
    ],
)
def test_keyword_maps_to_command(message: str, keyword: str) -> None:
    (ref,) = extract_refs(message, KEYS)
    assert ref.keyword == keyword


@pytest.mark.parametrize("text", [" @2h.", " @1h30m. Then", " @90m.\n", " @1.5h."])
def test_time_at_the_end_of_a_sentence(text: str) -> None:
    assert extract_time(text) is not None


def test_time_glued_to_a_decimal_is_not_logged() -> None:
    assert extract_time(" @2h.5") is None
