"""Response schemas of the AI spend reports (``GET .../ai-usage/report/``)."""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, Field

from specivo.core.exchange import ConversionSummary
from specivo.core.money import format_decimal_plain
from specivo.schemas.ai_usage import AiUsageTokens
from specivo.schemas.exchange_rate import RateUsedOut, UnconvertedGroupOut
from specivo.schemas.money import MoneyOut
from specivo.services.ai_spend_report_service import HIDDEN_ISSUE, ReportRow, SpendReport


class ReportAmountOut(BaseModel):
    """An amount in the reporting currency, plus what could not be converted (never dropped)."""

    total: MoneyOut
    unconverted: list[UnconvertedGroupOut]

    @classmethod
    def from_summary(cls, summary: ConversionSummary | None) -> ReportAmountOut | None:
        if summary is None:
            return None
        return cls(
            total=MoneyOut.from_money(summary.total.quantize()),
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
        )


class ReportRowOut(BaseModel):
    """One group of the report.

    ``key`` holds the grouping values (user and project ids, issue ids with
    ``0`` for an issue you cannot see, slugs and names; ``null`` for none),
    ``labels`` how to show them.
    """

    key: dict[str, Any]
    labels: dict[str, str]
    period: date | None = Field(description="First day of the period bucket, when grouped by a period")
    seat_only: bool = Field(description="A row holding only unallocated seat cost")
    entries: int
    unpriced_entries: int
    request_count: int
    tokens: AiUsageTokens = Field(description="Per class, the base and long-context buckets summed")
    total_tokens: int = Field(description="Every class of both buckets")
    marginal_spend: ReportAmountOut | None
    api_equivalent_value: ReportAmountOut | None
    allocated_seat_cost: ReportAmountOut | None = Field(description="Null when seat cost is not shown")
    unallocated_seat_cost: ReportAmountOut | None
    total_spend: ReportAmountOut | None = Field(description="Marginal spend plus seat cost")
    leverage: str | None = Field(description="API-equivalent value of subscription usage / seat cost")

    @classmethod
    def from_row(cls, row: ReportRow) -> ReportRowOut:
        return cls(
            key=row.key,
            labels=row.labels,
            period=row.period,
            seat_only=row.seat_only,
            entries=row.entries,
            unpriced_entries=row.unpriced_entries,
            request_count=row.request_count,
            tokens=AiUsageTokens(**row.tokens),
            total_tokens=row.total_tokens,
            marginal_spend=ReportAmountOut.from_summary(row.marginal),
            api_equivalent_value=ReportAmountOut.from_summary(row.api_equivalent),
            allocated_seat_cost=ReportAmountOut.from_summary(row.seat_allocated),
            unallocated_seat_cost=ReportAmountOut.from_summary(row.seat_unallocated),
            total_spend=ReportAmountOut.from_summary(row.total_spend),
            leverage=format_decimal_plain(row.leverage) if row.leverage is not None else None,
        )


class SpendReportOut(BaseModel):
    """An AI spend report: what was asked, the rows, the totals and the exchange rates used."""

    scope: str = Field(description="'instance' or 'project'")
    project_key: str | None
    date_from: date
    date_to: date
    group_by: list[str]
    period: str | None
    currency: str
    rate_policy: str
    include_seat_cost: bool
    seat_cost_shown: bool
    seat_cost_hidden: bool = Field(
        description="Seat cost was asked for but is left out: you cannot see every entry of the project"
    )
    unallocated_shown: bool
    sort: str
    hidden_issue_key: int = HIDDEN_ISSUE
    rows: list[ReportRowOut]
    totals: ReportRowOut
    rate_dates: list[date]
    rates_used: list[RateUsedOut]

    @classmethod
    def from_report(cls, report: SpendReport) -> SpendReportOut:
        return cls(
            scope=report.scope,
            project_key=report.project.key if report.project is not None else None,
            date_from=report.date_from,
            date_to=report.date_to,
            group_by=list(report.group_by),
            period=report.period,
            currency=report.currency,
            rate_policy=str(report.rate_policy),
            include_seat_cost=report.include_seat_cost,
            seat_cost_shown=report.seat_cost_shown,
            seat_cost_hidden=report.seat_cost_hidden,
            unallocated_shown=report.unallocated_shown,
            sort=report.sort,
            rows=[ReportRowOut.from_row(r) for r in report.rows],
            totals=ReportRowOut.from_row(report.totals),
            rate_dates=list(report.rate_dates),
            rates_used=[RateUsedOut.from_used(r) for r in report.rates_used],
        )
