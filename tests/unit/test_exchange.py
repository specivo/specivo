"""Currency conversion over a rate book: lookup order, policies, maximum age, precedence, exactness, summaries."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from specivo.core.exchange import (
    ConversionMethod,
    Converted,
    MissingReason,
    PolicyKind,
    RateBook,
    RateMissing,
    RateObservation,
    RatePolicy,
    summarize,
)
from specivo.core.money import Money

pytestmark = pytest.mark.unit

D = Decimal
DAY = date(2026, 11, 5)


def obs(day: date, base: str, quote: str, rate: str, source: str = "ecb") -> RateObservation:
    return RateObservation(day, base, quote, D(rate), source)


def book(*observations: RateObservation) -> RateBook:
    return RateBook(observations)


ENTRY = RatePolicy.entry_date(7)


# ---------------------------------------------------------------------------
# Lookup: direct, inverse, pivot
# ---------------------------------------------------------------------------


def test_same_currency_is_identity_without_rates() -> None:
    result = book().convert(Money("12.34", "USD"), "USD", DAY, ENTRY)

    assert isinstance(result, Converted)
    assert result.money == Money("12.34", "USD")
    assert result.rate.method is ConversionMethod.IDENTITY
    assert result.rate.rate == 1
    assert result.rate.rate_date is None


def test_direct_rate() -> None:
    result = book(obs(DAY, "USD", "THB", "32.45")).convert(Money("100", "USD"), "THB", DAY, ENTRY)

    assert isinstance(result, Converted)
    assert result.money == Money("3245", "THB")
    assert result.money.currency == "THB"
    assert result.rate.method is ConversionMethod.DIRECT
    assert result.rate.rate == D("32.45")
    assert result.rate.rate_date == DAY
    assert result.rate.legs[0].source == "ecb"
    assert result.rate.legs[0].inverted is False


def test_inverse_rate_is_one_over_the_stored_rate() -> None:
    result = book(obs(DAY, "EUR", "THB", "37.533")).convert(Money("37.533", "THB"), "EUR", DAY, ENTRY)

    assert isinstance(result, Converted)
    assert result.rate.method is ConversionMethod.INVERSE
    assert result.rate.legs[0].inverted is True
    assert result.money == Money("1.00000000", "EUR")


def test_triangulates_through_the_pivot() -> None:
    rates = book(obs(DAY, "EUR", "USD", "1.1224"), obs(DAY, "EUR", "THB", "37.533"))

    result = rates.convert(Money("100", "USD"), "THB", DAY, ENTRY, pivot="EUR")

    assert isinstance(result, Converted)
    assert result.rate.method is ConversionMethod.PIVOT
    assert [(leg.base, leg.quote, leg.inverted) for leg in result.rate.legs] == [
        ("USD", "EUR", True),
        ("EUR", "THB", False),
    ]
    # 100 / 1.1224 * 37.533, rounded half up to 8 places
    assert result.money.amount == D("3343.99501069")


def test_pivot_rate_date_is_the_older_leg() -> None:
    rates = book(obs(date(2026, 11, 3), "EUR", "USD", "1.12"), obs(DAY, "EUR", "THB", "37.5"))

    result = rates.convert(Money("1", "USD"), "THB", DAY, ENTRY)

    assert isinstance(result, Converted)
    assert result.rate.rate_date == date(2026, 11, 3)


def test_direct_beats_pivot_even_when_older() -> None:
    rates = book(
        obs(date(2026, 11, 1), "USD", "THB", "32", "manual"),
        obs(DAY, "EUR", "USD", "1.1224"),
        obs(DAY, "EUR", "THB", "37.533"),
    )

    result = rates.convert(Money("1", "USD"), "THB", DAY, ENTRY)

    assert isinstance(result, Converted)
    assert result.rate.method is ConversionMethod.DIRECT
    assert result.money == Money("32", "THB")


def test_pivot_is_not_used_when_one_side_is_the_pivot() -> None:
    result = book(obs(DAY, "USD", "THB", "32")).convert(Money("1", "EUR"), "THB", DAY, ENTRY, pivot="EUR")

    assert isinstance(result, RateMissing)
    assert result.reason is MissingReason.NO_RATE


def test_newer_inverse_beats_older_direct() -> None:
    rates = book(obs(date(2026, 11, 2), "USD", "THB", "30", "manual"), obs(DAY, "THB", "USD", "0.025"))

    result = rates.convert(Money("1", "USD"), "THB", DAY, ENTRY)

    assert isinstance(result, Converted)
    assert result.rate.method is ConversionMethod.INVERSE
    assert result.money == Money("40", "THB")


def test_same_day_direct_wins_a_tie_with_inverse() -> None:
    rates = book(obs(DAY, "USD", "THB", "32", "csv"), obs(DAY, "THB", "USD", "0.025", "csv"))

    result = rates.convert(Money("1", "USD"), "THB", DAY, ENTRY)

    assert isinstance(result, Converted)
    assert result.rate.method is ConversionMethod.DIRECT


# ---------------------------------------------------------------------------
# Precedence by source
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sources", "expected"),
    [
        (("ecb", "csv", "manual"), "manual"),
        (("ecb", "csv"), "csv"),
        (("ecb",), "ecb"),
    ],
)
def test_same_day_precedence_manual_then_csv_then_ecb(sources: tuple[str, ...], expected: str) -> None:
    values = {"manual": "31", "csv": "32", "ecb": "33"}
    rates = book(*(obs(DAY, "USD", "THB", values[s], s) for s in sources))

    result = rates.convert(Money("1", "USD"), "THB", DAY, ENTRY)

    assert isinstance(result, Converted)
    assert result.rate.legs[0].source == expected
    assert result.money == Money(values[expected], "THB")


def test_manual_inverse_beats_fetched_direct_on_the_same_day() -> None:
    rates = book(obs(DAY, "EUR", "THB", "37.5", "ecb"), obs(DAY, "THB", "EUR", "0.025", "manual"))

    result = rates.convert(Money("1", "EUR"), "THB", DAY, ENTRY)

    assert isinstance(result, Converted)
    assert result.rate.legs[0].source == "manual"
    assert result.money == Money("40", "THB")


def test_newer_day_beats_source_precedence() -> None:
    rates = book(obs(date(2026, 11, 4), "USD", "THB", "31", "manual"), obs(DAY, "USD", "THB", "33", "ecb"))

    result = rates.convert(Money("1", "USD"), "THB", DAY, ENTRY)

    assert isinstance(result, Converted)
    assert result.rate.legs[0].source == "ecb"


# ---------------------------------------------------------------------------
# Policies and maximum age
# ---------------------------------------------------------------------------


def test_entry_date_uses_the_newest_rate_on_or_before_the_date() -> None:
    rates = book(
        obs(date(2026, 11, 2), "USD", "THB", "31"),
        obs(date(2026, 11, 4), "USD", "THB", "32"),
        obs(date(2026, 11, 6), "USD", "THB", "33"),
    )

    result = rates.convert(Money("1", "USD"), "THB", DAY, ENTRY)

    assert isinstance(result, Converted)
    assert result.rate.rate_date == date(2026, 11, 4)
    assert result.money == Money("32", "THB")


def test_rate_exactly_max_age_old_is_used() -> None:
    rates = book(obs(date(2026, 10, 29), "USD", "THB", "32"))

    assert isinstance(rates.convert(Money("1", "USD"), "THB", DAY, ENTRY), Converted)


def test_rate_older_than_max_age_is_stale() -> None:
    rates = book(obs(date(2026, 10, 28), "USD", "THB", "32"))

    result = rates.convert(Money("5", "USD"), "THB", DAY, ENTRY)

    assert isinstance(result, RateMissing)
    assert result.reason is MissingReason.STALE
    assert result.newest_rate_date == date(2026, 10, 28)
    assert result.original == Money("5", "USD")
    assert result.to_currency == "THB"
    assert result.on == DAY


def test_max_age_zero_needs_a_rate_of_the_same_day() -> None:
    rates = book(obs(date(2026, 11, 4), "USD", "THB", "32"))

    result = rates.convert(Money("1", "USD"), "THB", DAY, RatePolicy.entry_date(0))

    assert isinstance(result, RateMissing)
    assert result.reason is MissingReason.STALE


def test_only_future_rates_means_no_rate() -> None:
    rates = book(obs(date(2026, 11, 6), "USD", "THB", "32"))

    result = rates.convert(Money("1", "USD"), "THB", DAY, ENTRY)

    assert isinstance(result, RateMissing)
    assert result.reason is MissingReason.NO_RATE
    assert result.newest_rate_date is None


def test_latest_uses_the_newest_rate_whatever_its_age_or_date() -> None:
    rates = book(obs(date(2025, 1, 1), "USD", "THB", "34"), obs(date(2026, 12, 1), "USD", "THB", "33"))

    old_entry = rates.convert(Money("1", "USD"), "THB", date(2024, 6, 1), RatePolicy.latest())
    stale_under_entry = rates.convert(Money("1", "USD"), "THB", date(2024, 6, 1), ENTRY)

    assert isinstance(old_entry, Converted)
    assert old_entry.rate.rate_date == date(2026, 12, 1)
    assert isinstance(stale_under_entry, RateMissing)
    assert stale_under_entry.reason is MissingReason.NO_RATE


def test_latest_versus_entry_date_pick_different_rates() -> None:
    rates = book(obs(date(2026, 11, 1), "USD", "THB", "31"), obs(date(2026, 11, 20), "USD", "THB", "35"))

    by_entry = rates.convert(Money("10", "USD"), "THB", DAY, ENTRY)
    by_latest = rates.convert(Money("10", "USD"), "THB", DAY, RatePolicy.latest())

    assert isinstance(by_entry, Converted) and isinstance(by_latest, Converted)
    assert by_entry.money == Money("310", "THB")
    assert by_latest.money == Money("350", "THB")


def test_fixed_date_policy_ignores_the_amount_date() -> None:
    rates = book(obs(date(2026, 10, 31), "USD", "THB", "31"), obs(DAY, "USD", "THB", "32"))
    policy = RatePolicy.fixed(date(2026, 10, 31))

    result = rates.convert(Money("1", "USD"), "THB", DAY, policy)

    assert isinstance(result, Converted)
    assert result.rate.rate_date == date(2026, 10, 31)


def test_no_rate_at_all() -> None:
    result = book(obs(DAY, "EUR", "USD", "1.1")).convert(Money("1", "JPY"), "THB", DAY, ENTRY)

    assert isinstance(result, RateMissing)
    assert result.reason is MissingReason.NO_RATE


@pytest.mark.parametrize(
    ("text", "kind", "fixed"),
    [
        (None, PolicyKind.ENTRY_DATE, None),
        ("", PolicyKind.ENTRY_DATE, None),
        ("entry_date", PolicyKind.ENTRY_DATE, None),
        ("LATEST", PolicyKind.LATEST, None),
        ("2026-10-31", PolicyKind.FIXED, date(2026, 10, 31)),
    ],
)
def test_policy_parse(text: str | None, kind: PolicyKind, fixed: date | None) -> None:
    policy = RatePolicy.parse(text, 3)

    assert policy.kind is kind
    assert policy.fixed_on == fixed


@pytest.mark.parametrize("text", ["yesterday", "2026-13-01", "entry"])
def test_policy_parse_refuses_garbage(text: str) -> None:
    with pytest.raises(ValueError):
        RatePolicy.parse(text)


def test_policy_refuses_negative_age() -> None:
    with pytest.raises(ValueError):
        RatePolicy.entry_date(-1)


# ---------------------------------------------------------------------------
# Exactness
# ---------------------------------------------------------------------------


def test_rate_book_refuses_non_positive_rates_and_unknown_sources() -> None:
    with pytest.raises(ValueError):
        book(obs(DAY, "USD", "THB", "0"))
    with pytest.raises(ValueError):
        book(obs(DAY, "USD", "THB", "1", "bank"))


def test_float_amounts_never_enter() -> None:
    with pytest.raises(TypeError):
        Money(0.1, "USD")  # type: ignore[arg-type]


def test_inversion_keeps_precision_beyond_the_default_context() -> None:
    rates = book(obs(DAY, "USD", "THB", "3"))

    result = rates.convert(Money("1", "THB"), "USD", DAY, ENTRY)

    assert isinstance(result, Converted)
    assert isinstance(result.rate.rate, Decimal)
    assert len(result.rate.rate.as_tuple().digits) == 40
    assert result.money.amount == D("0.33333333")


def test_converted_amount_rounds_half_up_at_eight_places() -> None:
    rates = book(obs(DAY, "USD", "THB", "0.000000005"))

    result = rates.convert(Money("1", "USD"), "THB", DAY, ENTRY)

    assert isinstance(result, Converted)
    assert result.money.amount == D("0.00000001")


def test_twelve_decimal_rates_stay_exact() -> None:
    rates = book(obs(DAY, "USD", "IDR", "16712.123456789012"))

    result = rates.convert(Money("0.01", "USD"), "IDR", DAY, ENTRY)

    assert isinstance(result, Converted)
    assert result.money.amount == D("167.12123457")
    assert all(isinstance(x, Decimal) for x in (result.rate.rate, result.money.amount))


# ---------------------------------------------------------------------------
# Report contract
# ---------------------------------------------------------------------------


def test_summary_totals_converted_and_keeps_unconverted_per_currency_and_reason() -> None:
    rates = book(
        obs(DAY, "USD", "EUR", "0.9"),
        obs(date(2026, 10, 1), "THB", "EUR", "0.025"),
    )
    items = [
        (Money("10", "USD"), DAY),
        (Money("20", "USD"), DAY),
        (Money("5", "EUR"), DAY),
        (Money("100", "THB"), DAY),
        (Money("300", "THB"), DAY),
        (Money("1000", "JPY"), DAY),
    ]

    summary = summarize([rates.convert(m, "EUR", d, ENTRY) for m, d in items], "EUR")

    assert summary.total == Money("32", "EUR")
    assert summary.converted_count == 3
    assert summary.has_unconverted
    assert [(g.currency, g.reason, g.amount, g.count) for g in summary.unconverted] == [
        ("JPY", MissingReason.NO_RATE, Money("1000", "JPY"), 1),
        ("THB", MissingReason.STALE, Money("400", "THB"), 2),
    ]
    assert summary.unconverted[1].newest_rate_date == date(2026, 10, 1)
    assert summary.rate_dates == (DAY,)
    assert len(summary.rates_used) == 1


def test_summary_of_nothing_is_zero() -> None:
    summary = summarize([], "THB")

    assert summary.total == Money.zero("THB")
    assert summary.unconverted == ()
    assert not summary.has_unconverted


def test_summary_refuses_results_in_another_currency() -> None:
    result = book(obs(DAY, "USD", "THB", "32")).convert(Money("1", "USD"), "THB", DAY, ENTRY)

    with pytest.raises(ValueError):
        summarize([result], "EUR")
