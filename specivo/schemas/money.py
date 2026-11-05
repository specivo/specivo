"""Money and currency schemas.

Amounts cross the API as **strings** (``"12.50"``), never JSON numbers: a
client that parses a number into a float has already lost the exact value.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator, model_validator

from specivo.core.money import (
    Money,
    MoneyError,
    check_minor_units,
    format_decimal_plain,
    normalize_currency_code,
    to_decimal,
)

# An optional sign, digits, an optional fraction. No exponent, no spaces,
# no thousands separators: amounts are machine values here.
_AMOUNT_RE = re.compile(r"^-?\d{1,20}(\.\d{1,12})?$")

CurrencyCode = Annotated[
    StrictStr,
    Field(min_length=3, max_length=3, examples=["USD"], description="ISO 4217 alphabetic code"),
]


class MoneyIn(BaseModel):
    """An amount someone types, in a currency.

    ``amount`` is a decimal string with at most as many decimal places as the
    currency's ISO minor units (``"10.50"`` USD, ``"1500"`` JPY, ``"1.250"``
    BHD); trailing zeros do not count. Negative amounts are accepted here; a
    field that must be positive says so itself. Whether the currency is enabled
    on the instance is checked by the service that stores the value
    (``CurrencyService.require_selectable``), because that needs the database.
    """

    model_config = ConfigDict(extra="forbid")

    amount: StrictStr = Field(examples=["12.50"])
    currency: CurrencyCode

    @field_validator("currency")
    @classmethod
    def _currency(cls, v: str) -> str:
        try:
            return normalize_currency_code(v)
        except MoneyError as exc:
            raise ValueError(str(exc)) from None

    @field_validator("amount")
    @classmethod
    def _amount(cls, v: str) -> str:
        v = v.strip()
        if not _AMOUNT_RE.match(v):
            raise ValueError("amount must be a decimal string such as '12.50'")
        return v

    @model_validator(mode="after")
    def _minor_units(self) -> MoneyIn:
        try:
            check_minor_units(to_decimal(self.amount), self.currency)
        except MoneyError as exc:
            raise ValueError(str(exc)) from None
        return self

    def to_money(self) -> Money:
        return Money(Decimal(self.amount), self.currency)


class MoneyOut(BaseModel):
    """A stored or computed amount, at full precision, as a string."""

    amount: str = Field(examples=["12.34567890"])
    currency: str = Field(examples=["USD"])

    @classmethod
    def from_money(cls, money: Money) -> MoneyOut:
        return cls(amount=format_decimal_plain(money.amount), currency=money.currency)


class CurrencyOut(BaseModel):
    """A currency for pickers, with its name and symbol in the reader's language."""

    code: str
    name: str = Field(description="Localised name (CLDR), falling back to the English ISO name")
    symbol: str = Field(description="Localised symbol (CLDR); the code when there is none")
    minor_units: int | None = Field(description="ISO decimal places; null where ISO says N.A.")


class CurrencyAdminOut(CurrencyOut):
    """A currency as administrators see it."""

    iso_name: str = Field(description="English ISO 4217 name")
    numeric_code: int | None
    kind: str
    is_active: bool


class CurrencyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_active: bool


class ReportingCurrencyOut(BaseModel):
    code: str


class ReportingCurrencyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: CurrencyCode
