"""Exchange rate schemas. Rates and amounts cross the API as decimal strings, never JSON numbers."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from specivo.core.exchange import ConversionResult, ConversionSummary, Converted, RateLeg, RateUsed
from specivo.core.money import format_decimal_plain
from specivo.models.exchange_rate import ExchangeRate
from specivo.schemas.money import CurrencyCode, MoneyIn, MoneyOut

RateString = StrictStr


class ExchangeRateOut(BaseModel):
    """``1 base = rate quote`` on ``rate_date``."""

    id: int
    rate_date: date
    base: str
    quote: str
    rate: str = Field(examples=["36.512300000000"], description="Decimal string, 12 decimal places")
    source: Literal["manual", "csv", "ecb"]
    created_by_id: int | None
    created_at: datetime

    @classmethod
    def from_row(cls, row: ExchangeRate) -> ExchangeRateOut:
        return cls(
            id=row.id,
            rate_date=row.rate_date,
            base=row.base_code,
            quote=row.quote_code,
            rate=format_decimal_plain(row.rate),
            source=row.source,  # type: ignore[arg-type]
            created_by_id=row.created_by_id,
            created_at=row.created_at,
        )


class ExchangeRateCreate(BaseModel):
    """A manual rate. ``rate`` is a decimal string such as ``"36.5123"`` (at most 12 decimal places)."""

    model_config = ConfigDict(extra="forbid")

    rate_date: date
    base: CurrencyCode
    quote: CurrencyCode
    rate: RateString = Field(examples=["36.5123"])


class ExchangeRateUpdate(BaseModel):
    """Fields to change on a manual rate; omitted fields stay."""

    model_config = ConfigDict(extra="forbid")

    rate_date: date | None = None
    base: CurrencyCode | None = None
    quote: CurrencyCode | None = None
    rate: RateString | None = None


class ExchangeRateImportIn(BaseModel):
    """A CSV document (``date,base,quote,rate`` per line, header optional)."""

    model_config = ConfigDict(extra="forbid")

    csv: StrictStr = Field(examples=["date,base,quote,rate\n2026-11-05,USD,THB,32.45\n"])
    dry_run: bool = False


class CsvRowOut(BaseModel):
    line: int
    rate_date: date
    base: str
    quote: str
    rate: str
    action: Literal["create", "update", "unchanged"]


class CsvLineErrorOut(BaseModel):
    line: int = Field(description="1-based line number in the file; 0 for the file as a whole")
    code: str
    message: str


class ExchangeRateImportOut(BaseModel):
    dry_run: bool
    applied: bool = Field(description="True when the rates were written")
    rows: int
    created: int
    updated: int
    unchanged: int
    errors: list[CsvLineErrorOut]
    preview: list[CsvRowOut] = Field(description="The first 50 rows and what the import does with each")


class ExchangeRateSettingsOut(BaseModel):
    pivot: str = Field(description="Currency used to triangulate when no direct or inverse rate exists")
    max_age_days: int = Field(description="Oldest a rate may be under the entry_date and fixed-date policies")
    fetch_enabled: bool = Field(description="Daily fetch of the configured feed; off by default")
    fetch_url: str
    last_fetch: dict | None = Field(description="Outcome of the last fetch: at, ok, stored, skipped, rate_date, error")


class ExchangeRateSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pivot: CurrencyCode | None = None
    max_age_days: int | None = Field(None, ge=0, le=366)
    fetch_enabled: bool | None = None
    fetch_url: StrictStr | None = Field(None, max_length=500, description="Empty restores the ECB default")


class FetchResultOut(BaseModel):
    ok: bool
    stored: int
    skipped: int
    rate_date: date | None
    error: str | None


class RateLegOut(BaseModel):
    base: str
    quote: str
    rate: str
    rate_date: date
    source: str
    inverted: bool

    @classmethod
    def from_leg(cls, leg: RateLeg) -> RateLegOut:
        return cls(
            base=leg.base,
            quote=leg.quote,
            rate=format_decimal_plain(leg.rate),
            rate_date=leg.rate_date,
            source=leg.source,
            inverted=leg.inverted,
        )


class RateUsedOut(BaseModel):
    from_currency: str
    to_currency: str
    rate: str = Field(description="Effective rate: 1 from_currency = rate to_currency")
    rate_date: date | None
    method: Literal["identity", "direct", "inverse", "pivot"]
    legs: list[RateLegOut]

    @classmethod
    def from_used(cls, used: RateUsed) -> RateUsedOut:
        return cls(
            from_currency=used.from_currency,
            to_currency=used.to_currency,
            rate=format_decimal_plain(used.rate),
            rate_date=used.rate_date,
            method=used.method.value,  # type: ignore[arg-type]
            legs=[RateLegOut.from_leg(leg) for leg in used.legs],
        )


class ConvertIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    money: MoneyIn
    to: CurrencyCode
    on: date
    policy: StrictStr | None = Field(None, description="'entry_date' (default), 'latest' or a fixed date YYYY-MM-DD")


class ConvertOut(BaseModel):
    """Either ``converted`` (with the rate used) or an explicit ``unconverted`` reason. Never a silent zero."""

    status: Literal["converted", "unconverted"]
    original: MoneyOut
    on: date
    policy: str
    converted: MoneyOut | None = None
    rate: RateUsedOut | None = None
    reason: Literal["no_rate", "stale"] | None = None
    newest_rate_date: date | None = Field(default=None, description="For 'stale': the date of the newest rate found")

    @classmethod
    def from_result(cls, result: ConversionResult, policy: str) -> ConvertOut:
        if isinstance(result, Converted):
            return cls(
                status="converted",
                original=MoneyOut.from_money(result.original),
                on=result.on,
                policy=policy,
                converted=MoneyOut.from_money(result.money),
                rate=RateUsedOut.from_used(result.rate),
            )
        return cls(
            status="unconverted",
            original=MoneyOut.from_money(result.original),
            on=result.on,
            policy=policy,
            reason=result.reason.value,  # type: ignore[arg-type]
            newest_rate_date=result.newest_rate_date,
        )


class UnconvertedGroupOut(BaseModel):
    currency: str
    reason: Literal["no_rate", "stale"]
    amount: MoneyOut
    count: int
    newest_rate_date: date | None


class ConversionSummaryOut(BaseModel):
    """Report contract: the total of what converted, plus what did not, per currency and reason."""

    currency: str
    total: MoneyOut
    converted_count: int
    unconverted: list[UnconvertedGroupOut]
    rates_used: list[RateUsedOut]

    @classmethod
    def from_summary(cls, summary: ConversionSummary) -> ConversionSummaryOut:
        return cls(
            currency=summary.currency,
            total=MoneyOut.from_money(summary.total),
            converted_count=summary.converted_count,
            unconverted=[
                UnconvertedGroupOut(
                    currency=g.currency,
                    reason=g.reason.value,  # type: ignore[arg-type]
                    amount=MoneyOut.from_money(g.amount),
                    count=g.count,
                    newest_rate_date=g.newest_rate_date,
                )
                for g in summary.unconverted
            ],
            rates_used=[RateUsedOut.from_used(r) for r in summary.rates_used],
        )
