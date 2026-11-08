"""Billing rate and billable report schemas. Amounts cross the API as decimal strings, never JSON numbers."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, field_validator

from specivo.schemas.exchange_rate import ConversionSummaryOut
from specivo.schemas.money import MoneyIn, MoneyOut

if TYPE_CHECKING:
    from specivo.models.billing_rate import BillingRate
    from specivo.services.billing_rate_service import BillableReport


def _positive(value: MoneyIn | None) -> MoneyIn | None:
    if value is not None and Decimal(value.amount) <= 0:
        raise ValueError("hourly_rate must be greater than zero")
    return value


class BillingRateCreate(BaseModel):
    """A new rate. ``hourly_rate`` is an amount string within the currency's minor units (``"80.00"`` USD)."""

    model_config = ConfigDict(extra="forbid")

    user_id: int | None = None
    hourly_rate: MoneyIn
    effective_from: date

    _check_rate = field_validator("hourly_rate")(_positive)


class BillingRateUpdate(BaseModel):
    """Fields to change; omitted fields stay. The amount and its currency always change together."""

    model_config = ConfigDict(extra="forbid")

    hourly_rate: MoneyIn | None = None
    effective_from: date | None = None

    _check_rate = field_validator("hourly_rate")(_positive)


class BillingRateOut(BaseModel):
    id: int
    user_id: int | None
    project_id: int | None
    hourly_rate: MoneyOut
    effective_from: date
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, rate: BillingRate) -> BillingRateOut:
        return cls(
            id=rate.id,
            user_id=rate.user_id,
            project_id=rate.project_id,
            hourly_rate=MoneyOut.from_money(rate.rate),
            effective_from=rate.effective_from,
            created_at=rate.created_at,
            updated_at=rate.updated_at,
        )


class BillingReportEntryOut(BaseModel):
    time_entry_id: int
    user_id: int
    spent_on: date
    hours: Decimal
    rate_id: int
    rate: MoneyOut
    amount: MoneyOut


class UnratedEntryOut(BaseModel):
    time_entry_id: int
    user_id: int
    spent_on: date
    hours: Decimal


class CurrencySubtotalOut(BaseModel):
    currency: str
    hours: Decimal
    entry_count: int
    amount: MoneyOut


class BillingReportOut(BaseModel):
    """Billable time in a period.

    ``subtotals`` has one row per rate currency, exact. ``total`` converts them
    into the reporting ``currency`` per entry date; whatever could not be
    converted is listed in ``total.unconverted`` at its original amount, never
    dropped. Entries without an effective rate are in ``unrated``.
    """

    date_from: date
    date_to: date
    currency: str = Field(description="Reporting currency of the converted total")
    rated_hours: Decimal
    unrated_hours: Decimal
    subtotals: list[CurrencySubtotalOut]
    total: ConversionSummaryOut
    entries: list[BillingReportEntryOut]
    unrated: list[UnratedEntryOut]

    @classmethod
    def from_report(cls, report: BillableReport) -> BillingReportOut:
        return cls(
            date_from=report.date_from,
            date_to=report.date_to,
            currency=report.currency,
            rated_hours=report.rated_hours,
            unrated_hours=report.unrated_hours,
            subtotals=[
                CurrencySubtotalOut(
                    currency=s.currency,
                    hours=s.hours,
                    entry_count=s.entry_count,
                    amount=MoneyOut.from_money(s.amount),
                )
                for s in report.subtotals
            ],
            total=ConversionSummaryOut.from_summary(report.total),
            entries=[
                BillingReportEntryOut(
                    time_entry_id=e.time_entry_id,
                    user_id=e.user_id,
                    spent_on=e.spent_on,
                    hours=e.hours,
                    rate_id=e.rate_id,
                    rate=MoneyOut.from_money(e.rate),
                    amount=MoneyOut.from_money(e.amount),
                )
                for e in report.entries
            ],
            unrated=[
                UnratedEntryOut(time_entry_id=u.time_entry_id, user_id=u.user_id, spent_on=u.spent_on, hours=u.hours)
                for u in report.unrated
            ],
        )
