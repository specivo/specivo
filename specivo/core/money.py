"""The ``Money`` value type: an exact decimal amount in one ISO 4217 currency.

Conventions every money column and API follows (see ``specivo.models.money``
for the storage side):

- Amounts are :class:`~decimal.Decimal`, never ``float``. Constructing a
  ``Money`` from a float, or multiplying one by a float, raises ``TypeError``.
- Arithmetic is between amounts of the same currency only. Adding, subtracting
  or comparing amounts in different currencies raises
  :class:`CurrencyMismatchError`; converting needs an exchange rate, which is a
  separate service.
- Computed amounts keep their full precision. Rounding to the currency's minor
  units happens on display and in posted totals, with ``ROUND_HALF_UP`` (the
  rule the time tracking code uses), through :meth:`Money.quantize`.
- Amounts people type are validated to the currency's minor units on input
  (:func:`check_minor_units`), never silently rounded.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from functools import lru_cache
from typing import Any

from specivo.core.currency_data import CURRENCY_CODE_RE, is_known_currency, minor_units_for


class MoneyError(ValueError):
    """Base class for invalid money values and operations."""


class UnknownCurrencyError(MoneyError):
    """The code is not a well-formed ISO 4217 code in the reference list."""


class CurrencyMismatchError(MoneyError):
    """An operation combined amounts in two different currencies."""

    def __init__(self, left: str, right: str, operation: str) -> None:
        self.left = left
        self.right = right
        super().__init__(
            f"Cannot {operation} amounts in different currencies ({left} and {right}); convert one of them first"
        )


class MinorUnitsError(MoneyError):
    """An entered amount has more decimal places than its currency allows."""


Number = Decimal | int


def normalize_currency_code(code: object) -> str:
    """Return *code* as an upper-case ISO 4217 code, or raise :class:`UnknownCurrencyError`."""
    if not isinstance(code, str):
        raise UnknownCurrencyError(f"Currency code must be a string, got {type(code).__name__}")
    normalized = code.strip().upper()
    if not CURRENCY_CODE_RE.match(normalized):
        raise UnknownCurrencyError(f"Currency code must be three letters, got {code!r}")
    if not is_known_currency(normalized):
        raise UnknownCurrencyError(f"Unknown currency code {normalized!r}")
    return normalized


def to_decimal(value: object) -> Decimal:
    """Convert *value* (a ``Decimal``, ``int`` or numeric string) to a finite ``Decimal``.

    Floats and booleans are refused with ``TypeError``: a float has already lost
    the exact value, and a bool is never an amount.
    """
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"Money amounts must be Decimal, int or str, not {type(value).__name__}")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, str):
        try:
            result = Decimal(value.strip())
        except InvalidOperation:
            raise MoneyError(f"Not a decimal amount: {value!r}") from None
    else:
        raise TypeError(f"Money amounts must be Decimal, int or str, not {type(value).__name__}")
    if not result.is_finite():
        raise MoneyError(f"Money amounts must be finite, got {value!r}")
    return result


def decimal_places(amount: Decimal) -> int:
    """Number of significant decimal places of *amount* (``Decimal("1.50")`` has 1)."""
    exponent = amount.normalize().as_tuple().exponent
    return -exponent if isinstance(exponent, int) and exponent < 0 else 0


def check_minor_units(amount: Decimal, currency: str) -> None:
    """Raise :class:`MinorUnitsError` if *amount* is finer than *currency*'s minor units.

    Trailing zeros do not count (``"10.50"`` is fine for USD, ``"10.505"`` is
    not; ``"100.0"`` is fine for JPY). Codes whose minor units are "N.A." accept
    any precision.
    """
    allowed = minor_units_for(currency)
    if allowed is None:
        return
    if decimal_places(amount) > allowed:
        if allowed == 0:
            raise MinorUnitsError(f"{currency} amounts cannot have decimal places")
        raise MinorUnitsError(f"{currency} amounts allow at most {allowed} decimal places")


def format_decimal_plain(amount: Decimal) -> str:
    """Fixed-point string of *amount*, never in exponent notation (``1E+2`` -> ``"100"``)."""
    return format(amount, "f")


@dataclass(frozen=True, slots=True)
class Money:
    """An immutable amount in one currency.

    ``Money(Decimal("12.50"), "USD")``; the amount may also be an ``int`` or a
    numeric string, and the code is upper-cased and checked against the ISO
    4217 list. Equality is by value: ``Money("1.0", "USD") == Money("1.00",
    "USD")``.
    """

    amount: Decimal
    currency: str

    def __init__(self, amount: Decimal | int | str, currency: str) -> None:
        object.__setattr__(self, "amount", to_decimal(amount))
        object.__setattr__(self, "currency", normalize_currency_code(currency))

    # -- construction -------------------------------------------------------

    @classmethod
    def zero(cls, currency: str) -> Money:
        return cls(Decimal(0), currency)

    # -- facts ----------------------------------------------------------------

    @property
    def minor_units(self) -> int | None:
        """ISO minor units of the currency; ``None`` for "N.A." codes (metals, ``XXX``)."""
        return minor_units_for(self.currency)

    @property
    def amount_str(self) -> str:
        """The amount as a fixed-point string, at full precision (for JSON)."""
        return format_decimal_plain(self.amount)

    # -- rounding -------------------------------------------------------------

    def quantize(self, places: int | None = None) -> Money:
        """Round to *places* decimals (default: the currency's minor units), half up.

        A currency without minor units ("N.A.") is returned unchanged unless
        *places* is given.
        """
        digits = self.minor_units if places is None else places
        if digits is None:
            return self
        if digits < 0:
            raise ValueError("places must not be negative")
        exponent = Decimal(1).scaleb(-digits)
        return Money(self.amount.quantize(exponent, rounding=ROUND_HALF_UP), self.currency)

    # -- display --------------------------------------------------------------

    def format(self, locale: str = "en") -> str:
        """Localised display string, rounded half up to the currency's minor units.

        Uses the locale's CLDR currency pattern and symbol through Babel
        (``format(Money("1234.5", "THB"), "th")`` gives ``"฿1,234.50"``). Codes
        without ISO minor units use CLDR's precision.
        """
        return _format_money(self.amount, self.currency, self.minor_units, locale)

    def __str__(self) -> str:
        return f"{self.amount_str} {self.currency}"

    # -- arithmetic -----------------------------------------------------------

    def _same_currency(self, other: Money, operation: str) -> None:
        if other.currency != self.currency:
            raise CurrencyMismatchError(self.currency, other.currency, operation)

    def __add__(self, other: object) -> Money:
        if isinstance(other, Money):
            self._same_currency(other, "add")
            return Money(self.amount + other.amount, self.currency)
        return NotImplemented

    def __radd__(self, other: object) -> Money:
        # sum() starts from int 0; let it through so sum(amounts) works.
        if isinstance(other, int) and not isinstance(other, bool) and other == 0:
            return self
        return NotImplemented

    def __sub__(self, other: object) -> Money:
        if isinstance(other, Money):
            self._same_currency(other, "subtract")
            return Money(self.amount - other.amount, self.currency)
        return NotImplemented

    def __mul__(self, factor: object) -> Money:
        if isinstance(factor, Money):
            raise TypeError("Cannot multiply two money amounts")
        return Money(self.amount * _scalar(factor), self.currency)

    __rmul__ = __mul__

    def __truediv__(self, divisor: object) -> Money:
        if isinstance(divisor, Money):
            raise TypeError("Divide money by a number; for a ratio of two amounts use .ratio()")
        value = _scalar(divisor)
        if value == 0:
            raise ZeroDivisionError("Cannot divide money by zero")
        return Money(self.amount / value, self.currency)

    def ratio(self, other: Money) -> Decimal:
        """``self / other`` as a plain ``Decimal``; both in the same currency."""
        self._same_currency(other, "divide")
        if other.amount == 0:
            raise ZeroDivisionError("Cannot divide by a zero amount")
        return self.amount / other.amount

    def __neg__(self) -> Money:
        return Money(-self.amount, self.currency)

    def __pos__(self) -> Money:
        return self

    def __abs__(self) -> Money:
        return Money(abs(self.amount), self.currency)

    # No __bool__ on purpose: ``if price:`` must keep meaning "is set", even
    # for a zero amount. Test ``price.amount == 0`` instead.

    # -- comparison -----------------------------------------------------------

    def _compare(self, other: object, operation: str) -> Decimal:
        if not isinstance(other, Money):
            raise TypeError(f"Cannot compare Money with {type(other).__name__}")
        self._same_currency(other, operation)
        return other.amount

    def __lt__(self, other: object) -> bool:
        return self.amount < self._compare(other, "compare")

    def __le__(self, other: object) -> bool:
        return self.amount <= self._compare(other, "compare")

    def __gt__(self, other: object) -> bool:
        return self.amount > self._compare(other, "compare")

    def __ge__(self, other: object) -> bool:
        return self.amount >= self._compare(other, "compare")


def _scalar(value: Any) -> Decimal:
    if isinstance(value, Money):
        raise TypeError("Expected a number, got Money")
    return to_decimal(value)


def currency_display_name(code: str, locale: str, fallback: str | None = None) -> str:
    """The currency's name in *locale* from CLDR (``"Thai Baht"``, ``"บาท"``).

    Falls back to *fallback* (the stored English ISO name), then the code, for
    codes CLDR does not name or locales Babel cannot parse.
    """
    from babel.numbers import get_currency_name

    try:
        name = str(get_currency_name(code, locale=locale.replace("-", "_")))
    except Exception:
        name = code
    if name == code and fallback:
        return fallback
    return name


def currency_display_symbol(code: str, locale: str) -> str:
    """The currency's symbol in *locale* from CLDR (``"$"``, ``"฿"``); the code when there is none."""
    from babel.numbers import get_currency_symbol

    try:
        return str(get_currency_symbol(code, locale=locale.replace("-", "_")))
    except Exception:
        return code


@lru_cache(maxsize=64)
def _currency_pattern(locale: str) -> tuple[Any, Any]:
    from babel import Locale
    from babel.numbers import parse_pattern

    loc = Locale.parse(locale.replace("-", "_"))
    return loc, parse_pattern(loc.currency_formats["standard"])


def _format_money(amount: Decimal, currency: str, minor_units: int | None, locale: str) -> str:
    from babel.numbers import get_currency_precision

    digits = minor_units if minor_units is not None else get_currency_precision(currency)
    rounded = amount.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)
    try:
        loc, base_pattern = _currency_pattern(locale)
    except Exception:
        loc, base_pattern = _currency_pattern("en")
    # Fix the fraction digits to the ISO minor units: Babel's own currency
    # precision comes from CLDR and differs for some codes (IQD, LBP, ...).
    # The amount is already rounded, so Babel does not round it again.
    pattern = copy.copy(base_pattern)
    pattern.frac_prec = (digits, digits)
    return str(pattern.apply(rounded, loc, currency=currency, currency_digits=False))
