"""The ``Money`` value type, the money schemas and the money column helpers.

Pure logic: no database. Amounts in the examples are USD, EUR, THB, JPY and
BHD, which cover zero, two and three minor units.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import CHAR, Integer, Numeric
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.schema import CreateTable

from specivo.core.money import (
    CurrencyMismatchError,
    MinorUnitsError,
    Money,
    MoneyError,
    UnknownCurrencyError,
    check_minor_units,
    currency_display_name,
    currency_display_symbol,
)
from specivo.models.money import (
    amount_column,
    currency_column,
    money_numeric,
    money_or_none,
    money_pair_constraints,
)
from specivo.schemas.money import MoneyIn, MoneyOut

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_amount_is_kept_exact_from_decimal_int_and_string() -> None:
    assert Money(Decimal("12.345678901"), "USD").amount == Decimal("12.345678901")
    assert Money(7, "EUR").amount == Decimal(7)
    assert Money(" 0.10 ", "THB").amount == Decimal("0.10")


def test_currency_code_is_upper_cased() -> None:
    assert Money("1", "thb").currency == "THB"


@pytest.mark.parametrize("value", [1.5, 0.1, True])
def test_floats_and_bools_are_refused(value: object) -> None:
    with pytest.raises(TypeError):
        Money(value, "USD")  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["abc", "NaN", "Infinity", "-inf", ""])
def test_non_numeric_and_non_finite_amounts_are_refused(value: str) -> None:
    with pytest.raises(MoneyError):
        Money(value, "USD")


@pytest.mark.parametrize("code", ["US", "USDX", "12A", "", "ZZZ"])
def test_malformed_or_unknown_codes_are_refused(code: str) -> None:
    with pytest.raises(UnknownCurrencyError):
        Money("1", code)


def test_money_is_immutable() -> None:
    price = Money("1", "USD")
    with pytest.raises(AttributeError):
        price.amount = Decimal("2")  # type: ignore[misc]


def test_equality_and_hash_are_by_value() -> None:
    assert Money("1.0", "USD") == Money("1.00", "USD")
    assert hash(Money("1.0", "USD")) == hash(Money(1, "USD"))
    assert Money("1", "USD") != Money("1", "EUR")


def test_zero_amount_is_not_falsy_by_accident() -> None:
    # No __bool__: ``if price:`` means "is set", even when the amount is zero.
    assert Money.zero("USD")


# ---------------------------------------------------------------------------
# Arithmetic
# ---------------------------------------------------------------------------


def test_same_currency_arithmetic() -> None:
    a = Money("10.25", "EUR")
    b = Money("0.75", "EUR")
    assert a + b == Money("11.00", "EUR")
    assert a - b == Money("9.50", "EUR")
    assert -a == Money("-10.25", "EUR")
    assert abs(Money("-3", "EUR")) == Money("3", "EUR")
    assert a * 2 == Money("20.50", "EUR")
    assert 3 * b == Money("2.25", "EUR")
    assert a * Decimal("0.5") == Money("5.125", "EUR")
    assert Money("10", "EUR") / 4 == Money("2.5", "EUR")
    assert a.ratio(b) == Decimal("10.25") / Decimal("0.75")


def test_sum_of_amounts() -> None:
    assert sum([Money("1.10", "THB"), Money("2.20", "THB"), Money("3.30", "THB")]) == Money("6.60", "THB")


def test_computed_amounts_keep_full_precision() -> None:
    # A per-token cost: 1234 tokens at 3.00 per million.
    cost = Money("3.00", "USD") * 1234 / 1_000_000
    assert cost.amount == Decimal("0.003702")


@pytest.mark.parametrize(
    "operation",
    [
        lambda a, b: a + b,
        lambda a, b: a - b,
        lambda a, b: a < b,
        lambda a, b: a >= b,
        lambda a, b: a.ratio(b),
        lambda a, b: sum([a, b]),
    ],
)
def test_mixing_currencies_raises(operation) -> None:
    with pytest.raises(CurrencyMismatchError) as exc:
        operation(Money("1", "USD"), Money("1", "THB"))
    assert "USD" in str(exc.value) and "THB" in str(exc.value)


def test_mixing_currencies_is_a_value_error_with_both_codes() -> None:
    with pytest.raises(ValueError):
        Money("1", "USD") + Money("1", "EUR")
    try:
        Money("1", "USD") + Money("1", "EUR")
    except CurrencyMismatchError as exc:
        assert (exc.left, exc.right) == ("USD", "EUR")


def test_money_cannot_be_multiplied_by_money_or_by_a_float() -> None:
    with pytest.raises(TypeError):
        Money("1", "USD") * Money("1", "USD")
    with pytest.raises(TypeError):
        Money("1", "USD") * 1.5
    with pytest.raises(TypeError):
        Money("1", "USD") / Money("1", "USD")
    with pytest.raises(TypeError):
        Money("1", "USD") + 1  # type: ignore[operator]


def test_division_by_zero_raises() -> None:
    with pytest.raises(ZeroDivisionError):
        Money("1", "USD") / 0
    with pytest.raises(ZeroDivisionError):
        Money("1", "USD").ratio(Money.zero("USD"))


def test_comparison_within_a_currency() -> None:
    assert Money("1.99", "USD") < Money("2", "USD") <= Money("2.00", "USD")
    assert Money("5", "JPY") > Money("4", "JPY")
    with pytest.raises(TypeError):
        _ = Money("1", "USD") < 2


# ---------------------------------------------------------------------------
# Quantize: ISO minor units, half up
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("amount", "currency", "expected"),
    [
        ("1234.5", "JPY", "1235"),  # 0 minor units
        ("1234.4", "JPY", "1234"),
        ("10.005", "USD", "10.01"),  # 2 minor units, half up (half-even would give 10.00)
        ("10.0049", "USD", "10.00"),
        ("2.675", "USD", "2.68"),
        ("1.2345", "BHD", "1.235"),  # 3 minor units
        ("1.2344", "BHD", "1.234"),
        ("-10.005", "USD", "-10.01"),  # half up away from zero
    ],
)
def test_quantize_to_minor_units_half_up(amount: str, currency: str, expected: str) -> None:
    result = Money(amount, currency).quantize()
    assert result == Money(expected, currency)
    assert result.amount_str == expected


def test_minor_units_come_from_iso() -> None:
    assert Money("1", "JPY").minor_units == 0
    assert Money("1", "USD").minor_units == 2
    assert Money("1", "BHD").minor_units == 3
    assert Money("1", "CLF").minor_units == 4
    assert Money("1", "XAU").minor_units is None


def test_quantize_leaves_codes_without_minor_units_alone_unless_told() -> None:
    gold = Money("1.23456", "XAU")
    assert gold.quantize() is gold
    assert gold.quantize(2) == Money("1.23", "XAU")


def test_quantize_returns_a_new_value() -> None:
    price = Money("10.005", "USD")
    price.quantize()
    assert price.amount == Decimal("10.005")


def test_amount_str_is_never_in_exponent_notation() -> None:
    assert Money(Decimal("1E+3"), "USD").amount_str == "1000"
    assert Money(Decimal("1E-8"), "USD").amount_str == "0.00000001"
    assert str(Money("12.5", "EUR")) == "12.5 EUR"


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("amount", "currency", "locale", "expected"),
    [
        ("1234.5", "USD", "en", "$1,234.50"),
        ("1234.5", "THB", "th", "฿1,234.50"),
        ("1234.5", "USD", "th", "US$1,234.50"),
        ("1234.5", "JPY", "en", "¥1,235"),
        ("10.005", "USD", "en", "$10.01"),
        ("-1.2345", "BHD", "en", "-BHD1.235"),
        # CLDR shows IQD without decimals; the ISO minor units (3) win.
        ("1", "IQD", "en", "IQD1.000"),
    ],
)
def test_format_uses_locale_and_iso_minor_units(amount: str, currency: str, locale: str, expected: str) -> None:
    assert Money(amount, currency).format(locale) == expected


def test_format_falls_back_to_english_for_an_unknown_locale() -> None:
    assert Money("1", "USD").format("not-a-locale") == "$1.00"


def test_localised_names_and_symbols() -> None:
    assert currency_display_name("THB", "en") == "Thai Baht"
    assert currency_display_name("THB", "th") == "บาท"
    assert currency_display_symbol("THB", "th") == "฿"
    assert currency_display_symbol("EUR", "en") == "€"
    assert currency_display_name("XTS", "en", fallback="Testing code") != ""


# ---------------------------------------------------------------------------
# Input precision
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("amount", "currency"),
    [("10.50", "USD"), ("10.5", "USD"), ("100.0", "JPY"), ("1500", "JPY"), ("1.250", "BHD"), ("0.1234", "CLF")],
)
def test_amounts_within_minor_units_pass(amount: str, currency: str) -> None:
    check_minor_units(Decimal(amount), currency)


@pytest.mark.parametrize(("amount", "currency"), [("10.505", "USD"), ("1.5", "JPY"), ("1.2345", "BHD")])
def test_amounts_finer_than_minor_units_are_refused(amount: str, currency: str) -> None:
    with pytest.raises(MinorUnitsError):
        check_minor_units(Decimal(amount), currency)


def test_codes_without_minor_units_accept_any_precision() -> None:
    check_minor_units(Decimal("1.123456"), "XAU")


# ---------------------------------------------------------------------------
# MoneyIn / MoneyOut
# ---------------------------------------------------------------------------


def test_money_in_accepts_a_string_amount_and_normalises_the_code() -> None:
    value = MoneyIn.model_validate({"amount": "12.50", "currency": "eur"})
    assert value.currency == "EUR"
    assert value.to_money() == Money("12.50", "EUR")


@pytest.mark.parametrize("amount", [12.5, 12, None])
def test_money_in_refuses_json_numbers(amount: object) -> None:
    with pytest.raises(ValidationError):
        MoneyIn.model_validate({"amount": amount, "currency": "USD"})


@pytest.mark.parametrize("amount", ["1e3", "1,000.00", "12.5.1", " ", "abc", "+1"])
def test_money_in_refuses_malformed_amounts(amount: str) -> None:
    with pytest.raises(ValidationError):
        MoneyIn.model_validate({"amount": amount, "currency": "USD"})


@pytest.mark.parametrize(("amount", "currency"), [("10.505", "USD"), ("1.5", "JPY"), ("1.2345", "BHD")])
def test_money_in_validates_minor_units(amount: str, currency: str) -> None:
    with pytest.raises(ValidationError) as exc:
        MoneyIn.model_validate({"amount": amount, "currency": currency})
    assert "decimal places" in str(exc.value)


@pytest.mark.parametrize("currency", ["ZZZ", "US", "usdx", 840])
def test_money_in_refuses_unknown_currencies(currency: object) -> None:
    with pytest.raises(ValidationError):
        MoneyIn.model_validate({"amount": "1", "currency": currency})


def test_money_in_refuses_extra_fields() -> None:
    with pytest.raises(ValidationError):
        MoneyIn.model_validate({"amount": "1", "currency": "USD", "rate": "2"})


def test_money_out_serialises_the_amount_as_a_plain_string() -> None:
    out = MoneyOut.from_money(Money(Decimal("0.00370200"), "USD"))
    assert out.model_dump() == {"amount": "0.00370200", "currency": "USD"}
    assert MoneyOut.from_money(Money(Decimal("1E+2"), "THB")).amount == "100"


# ---------------------------------------------------------------------------
# Column helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("kind", "precision", "scale"), [("amount", 20, 8), ("unit_price", 18, 6), ("rate", 24, 12)])
def test_money_numeric_scales(kind, precision: int, scale: int) -> None:
    numeric = money_numeric(kind)
    assert isinstance(numeric, Numeric)
    assert (numeric.precision, numeric.scale, numeric.asdecimal) == (precision, scale, True)


def test_money_pair_ddl() -> None:
    # A throwaway declarative base, so the sample table stays out of the app's metadata.
    class SampleBase(DeclarativeBase):
        pass

    class SampleCurrency(SampleBase):
        __tablename__ = "currencies"
        code: Mapped[str] = mapped_column(CHAR(3), primary_key=True)

    class SamplePrice(SampleBase):
        __tablename__ = "sample_prices"
        __table_args__ = (*money_pair_constraints("sample_prices", "price", nullable=True),)
        id: Mapped[int] = mapped_column(Integer, primary_key=True)
        price_amount: Mapped[Decimal | None] = amount_column("unit_price", nullable=True)
        price_currency: Mapped[str | None] = currency_column(nullable=True)

    table = SamplePrice.__table__
    ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))  # type: ignore[arg-type]

    assert "price_amount NUMERIC(18, 6)" in ddl
    assert "price_currency CHAR(3)" in ddl
    assert "FOREIGN KEY(price_currency) REFERENCES currencies (code) ON DELETE RESTRICT" in ddl
    assert "CONSTRAINT ck_sample_prices_price_money_pair CHECK" in ddl
    assert {i.name for i in table.indexes} == {"ix_sample_prices_price_currency"}  # type: ignore[attr-defined]


def test_required_pair_has_no_pair_check() -> None:
    items = money_pair_constraints("fees", "total", nullable=False)
    assert [type(i).__name__ for i in items] == ["Index"]


def test_money_or_none() -> None:
    assert money_or_none(Decimal("1.5"), "EUR") == Money("1.5", "EUR")
    assert money_or_none(None, "EUR") is None
    assert money_or_none(Decimal("1"), None) is None
