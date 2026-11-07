"""AI spend reports: AI usage grouped and totalled in one reporting currency, with seat cost allocated.

:meth:`AiSpendReportService.report` answers "who, which models, which tools,
which projects and issues, and how much" for an instance (administrators) or
one project, over a date range, grouped by any of :data:`GROUP_BY` plus an
optional period bucket (:data:`PERIODS`; coarsened past :data:`MAX_PERIODS`
buckets, so no caller can ask for a day per row over three years).

Rows
----
Only entries the viewer may read (``AiUsageService`` visibility) count;
excluded entries never do. Each row carries the token sums per class, the
requests and entries, and four amounts converted into the reporting currency:

- **marginal spend**: what the usage cost on top of flat-rate plans (the
  entries' snapshot, :mod:`specivo.core.ai_usage`);
- **API-equivalent value**: the same tokens at pay-per-use prices;
- **allocated seat cost**: the share of subscription fees carried by the
  row's entries (below);
- **unallocated seat cost**: fees no entry carries (instance reports only);

plus **total spend** = marginal + seat cost, and **leverage** = the
API-equivalent value of subscription usage / the seat cost, where a seat cost
exists. Amounts without a usable exchange rate are listed per currency and
reason (``ConversionSummary.unconverted``), never dropped; the rates used are
listed with their dates.

Seat allocation
---------------
For each user, month and subscription, the month's prorated fee
(:func:`specivo.core.ai_subscriptions.prorated_fee`, the S6 rule) is spread
over that user's ``subscription`` entries of the month linked to that
subscription, weighted by their API-equivalent value converted into the
subscription's currency (at each entry's date). The weights use **all** of the
user's counted entries of the month, whatever the viewer may see and whatever
the report period, so an entry's share never depends on who looks or which
dates the report covers. A month cut by the report period counts the shares of
the entries inside it.

When no entry carries a fee (no linked entries, or none with a value that
converts), the fee is **unallocated**. Unallocated seat cost appears only in
instance reports with no filter that a fee cannot match (tool, model,
provider); within the report period it counts the days the period covers.

Seat fees in another currency than the reporting currency are converted at the
rate of the month's last day (today for the running month) under the default
``entry_date`` policy; ``latest`` and a fixed date apply as for any amount.
See :data:`SEAT_RATE_DAY`.

Who sees seat cost
------------------
Subscriptions are private to their holder and administrators. Allocated seat
cost is shown in a project report only to a viewer who reads every entry of
the project (an administrator, or visibility ``all`` with every issue
visible); otherwise the seat measures are left out and ``seat_cost_hidden`` is
set. Unallocated seat cost and fees never appear in project reports, so a
project viewer can see at most the shares carried by that project's entries,
never another user's whole fee. No plan name or subscription detail is part of
a report. :meth:`AiSpendReportService.seat_breakdown` groups the same numbers
by plan id too, for the charts (``ai_usage_chart_service``), which name plans
on the instance report only.

Query count
-----------
Bounded whatever the data volume: one grouped query for the rows, one for the
subscriptions and one for the allocation weights, the label lookups (users,
projects, issues, tools), and at most three queries per currency conversion
target (the reporting currency, plus one per subscription currency the weights
must be converted into).
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.ai_pricing import TOKEN_CLASSES, normalize_provider
from specivo.core.ai_subscriptions import month_first, month_last, months_between, prorated_fee
from specivo.core.exceptions import AppError, PermissionDeniedError
from specivo.core.exchange import (
    ConversionResult,
    ConversionSummary,
    Converted,
    PolicyKind,
    RatePolicy,
    summarize,
)
from specivo.core.money import Money, format_decimal_plain
from specivo.core.utils import utcnow
from specivo.models.ai_subscription import AiSubscription
from specivo.models.ai_usage_entry import AI_USAGE_COST_TYPES, AiUsageEntry
from specivo.models.issue import Issue
from specivo.models.project import Project
from specivo.models.role import AI_USAGE_VISIBILITY_ALL
from specivo.models.user import User
from specivo.services.ai_usage_service import AiUsageService, unpriced_models
from specivo.services.api_key_scope import is_global_admin

#: Dimensions a report can be grouped by, in their canonical order.
GROUP_BY: tuple[str, ...] = ("user", "project", "issue", "tool", "provider", "model", "cost_type", "branch")
#: Period buckets: a UTC day, an ISO week (from Monday) or a calendar month.
PERIODS: tuple[str, ...] = ("day", "week", "month")
#: Row orders: by the grouping key (default), or by total spend, largest first.
SORTS: tuple[str, ...] = ("key", "spend")
#: Longest report range, in days (about three years).
MAX_RANGE_DAYS = 1100
#: Most period buckets a report has: a finer period over a longer range is
#: coarsened (day, then week, then month), whoever asks.
MAX_PERIODS = 120

#: Which day a month's seat fee is converted at under the ``entry_date`` policy:
#: ``"month_end"`` (the month's last day, or today for the running month). An
#: open decision; kept in one place so it is easy to change.
SEAT_RATE_DAY = "month_end"

#: Dimensions an unallocated seat fee can fill (the rest stay empty, so it gets a row of its own).
_SEAT_DIMENSIONS = frozenset({"user", "provider", "cost_type", "subscription"})
#: Column of each dimension.
_COLUMNS = {
    "user": AiUsageEntry.user_id,
    "project": AiUsageEntry.project_id,
    "issue": AiUsageEntry.issue_id,
    "tool": AiUsageEntry.tool_slug,
    "provider": AiUsageEntry.provider,
    "model": AiUsageEntry.model,
    "cost_type": AiUsageEntry.cost_type,
    "branch": AiUsageEntry.git_branch,
    # Internal only (never in GROUP_BY, so no report groups by plan): the charts' seat breakdown.
    "subscription": AiUsageEntry.subscription_id,
}
#: Issue key of entries on an issue the viewer cannot see (their own entries only).
HIDDEN_ISSUE = 0

_SHARE_EXPONENT = Decimal("0.00000001")


def _invalid(message: str, field_name: str) -> AppError:
    return AppError(code="ai_spend_report_invalid", message=message, status_code=422, field=field_name)


def period_start(day: date, period: str | None) -> date | None:
    """The first day of the *period* bucket holding *day* (None without a period)."""
    if period is None:
        return None
    if period == "day":
        return day
    if period == "week":
        return day - timedelta(days=day.weekday())
    return day.replace(day=1)


def period_end(start: date, period: str) -> date:
    """The last day of the *period* bucket starting on *start*."""
    if period == "day":
        return start
    if period == "week":
        return start + timedelta(days=6)
    return month_last(start.year, start.month)


def coarsen_period(date_from: date, date_to: date, period: str | None) -> str | None:
    """*period*, or the next coarser one while the range would have more than :data:`MAX_PERIODS` buckets."""
    from specivo.services.ai_usage_chart_service import bucket_count

    if period is None:
        return None
    for size in PERIODS[PERIODS.index(period) :]:
        if bucket_count(date_from, date_to, size) <= MAX_PERIODS:
            return size
    return PERIODS[-1]


def parse_group_by(value: str | Iterable[str] | None) -> tuple[str, ...]:
    """Dimensions from ``"user,model"`` or a list; unknown or repeated ones are a 422. Canonical order."""
    if value is None:
        return ()
    items = value.split(",") if isinstance(value, str) else list(value)
    names = [str(v).strip().lower() for v in items if str(v).strip()]
    unknown = [n for n in names if n not in GROUP_BY]
    if unknown:
        raise _invalid(f"group_by takes {', '.join(GROUP_BY)}; not {', '.join(unknown)}", "group_by")
    if len(set(names)) != len(names):
        raise _invalid("Name each group_by dimension once", "group_by")
    return tuple(n for n in GROUP_BY if n in names)


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReportFilters:
    """Narrow the entries a report counts (all optional)."""

    user_id: int | None = None
    tool: str | None = None
    model: str | None = None
    provider: str | None = None
    cost_type: str | None = None

    @property
    def excludes_seats(self) -> bool:
        """Whether a fee cannot match these filters (it has no tool or model)."""
        return bool(self.tool or self.model or self.provider) or self.cost_type not in (None, "subscription")


@dataclass
class ReportRow:
    """One group of a report: its key, measures and amounts in the reporting currency.

    ``key`` maps each grouping dimension to its value (user and project ids,
    issue ids with :data:`HIDDEN_ISSUE` for issues the viewer cannot see,
    slugs and names; ``None`` for none). ``seat_only`` marks a row holding only
    unallocated seat cost, grouped by dimensions a fee cannot fill.
    """

    key: dict[str, Any]
    period: date | None
    seat_only: bool = False
    labels: dict[str, str] = field(default_factory=dict)
    entries: int = 0
    unpriced_entries: int = 0
    request_count: int = 0
    #: Per class, both token buckets (base and long-context) summed.
    tokens: dict[str, int] = field(default_factory=lambda: dict.fromkeys(TOKEN_CLASSES, 0))
    marginal: ConversionSummary | None = None
    api_equivalent: ConversionSummary | None = None
    seat_allocated: ConversionSummary | None = None
    seat_unallocated: ConversionSummary | None = None
    total_spend: ConversionSummary | None = None
    #: The API-equivalent value of the row's subscription usage (the leverage numerator), with seat cost only.
    seat_api: ConversionSummary | None = None
    leverage: Decimal | None = None
    # Conversion results gathered while the rows are built.
    _results: dict[str, list[ConversionResult]] = field(default_factory=lambda: defaultdict(list))

    @property
    def total_tokens(self) -> int:
        return sum(self.tokens.values())


@dataclass
class SpendReport:
    """A whole report: what was asked, the rows and the totals."""

    scope: str
    project: Project | None
    date_from: date
    date_to: date
    group_by: tuple[str, ...]
    period: str | None
    currency: str
    rate_policy: RatePolicy
    include_seat_cost: bool
    seat_cost_shown: bool
    seat_cost_hidden: bool
    unallocated_shown: bool
    sort: str
    filters: ReportFilters
    rows: list[ReportRow]
    totals: ReportRow
    #: Models of the unpriced entries counted, most entries first (a few).
    unpriced_models: list[tuple[str, int]] = field(default_factory=list)
    #: The period asked for, when it was coarsened into ``period`` (see :func:`coarsen_period`).
    requested_period: str | None = None

    @property
    def rate_dates(self) -> tuple[date, ...]:
        dates: set[date] = set()
        for summary in (self.totals.marginal, self.totals.api_equivalent, self.totals.total_spend):
            if summary is not None:
                dates.update(summary.rate_dates)
        return tuple(sorted(dates))

    @property
    def rates_used(self) -> list[Any]:
        seen: dict[tuple[Any, ...], Any] = {}
        for summary in (self.totals.marginal, self.totals.api_equivalent, self.totals.total_spend):
            for used in summary.rates_used if summary is not None else ():
                seen.setdefault((used.from_currency, used.to_currency, used.rate, used.rate_date, used.method), used)
        return sorted(seen.values(), key=lambda r: (r.from_currency, r.rate_date or date.min))


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class AiSpendReportService:
    """Build AI spend reports (see the module docstring)."""

    def __init__(self) -> None:
        self._usage = AiUsageService()

    async def report(
        self,
        session: AsyncSession,
        viewer: User,
        *,
        project: Project | None,
        date_from: date,
        date_to: date,
        group_by: Sequence[str] = ("user",),
        period: str | None = None,
        currency: str | None = None,
        rate_policy: str | RatePolicy | None = None,
        include_seat_cost: bool = True,
        filters: ReportFilters | None = None,
        sort: str = "key",
    ) -> SpendReport:
        """The report of *project* (None: the whole instance, administrators only) from *date_from* to *date_to*.

        Dates are UTC days, both included. *currency* defaults to the
        project's (or the instance's) reporting currency; *rate_policy* to
        ``entry_date`` with the instance's maximum rate age. 403 for an
        instance report by anyone but an administrator; 422 for bad
        parameters.
        """
        return await self._report(
            session,
            viewer,
            project=project,
            date_from=date_from,
            date_to=date_to,
            dims=parse_group_by(group_by),
            period=period,
            currency=currency,
            rate_policy=rate_policy,
            include_seat_cost=include_seat_cost,
            filters=filters,
            sort=sort,
        )

    async def seat_breakdown(
        self,
        session: AsyncSession,
        viewer: User,
        *,
        project: Project | None,
        date_from: date,
        date_to: date,
        period: str,
        currency: str,
        rate_policy: str | RatePolicy | None = None,
        filters: ReportFilters | None = None,
    ) -> SpendReport:
        """The report rows per *period*, user and subscription, for the AI usage charts.

        The same rules as :meth:`report` (who may see seat cost, allocation,
        unallocated fees in instance reports), grouped by subscription too, so
        the charts can sum seat cost per period and per plan and person. Not a
        public grouping: plan ids never leave the server, the caller decides
        which plan details the reader may see.
        """
        return await self._report(
            session,
            viewer,
            project=project,
            date_from=date_from,
            date_to=date_to,
            dims=("user", "subscription"),
            period=period,
            currency=currency,
            rate_policy=rate_policy,
            include_seat_cost=True,
            filters=filters,
            sort="key",
        )

    async def _report(
        self,
        session: AsyncSession,
        viewer: User,
        *,
        project: Project | None,
        date_from: date,
        date_to: date,
        dims: tuple[str, ...],
        period: str | None,
        currency: str | None,
        rate_policy: str | RatePolicy | None,
        include_seat_cost: bool,
        filters: ReportFilters | None,
        sort: str,
    ) -> SpendReport:
        from specivo.services.currency_service import CurrencyService
        from specivo.services.exchange_rate_service import ExchangeRateService, parse_policy
        from specivo.services.project_service import ProjectService

        filters = filters or ReportFilters()
        if period is not None and period not in PERIODS:
            raise _invalid(f"period must be one of {', '.join(PERIODS)}", "period")
        if sort not in SORTS:
            raise _invalid(f"sort must be one of {', '.join(SORTS)}", "sort")
        if date_to < date_from:
            raise _invalid("The last day cannot be before the first day", "to_date")
        if (date_to - date_from).days + 1 > MAX_RANGE_DAYS:
            raise _invalid(f"A report covers at most {MAX_RANGE_DAYS} days", "to_date")
        if filters.cost_type is not None and filters.cost_type not in AI_USAGE_COST_TYPES:
            raise _invalid(f"cost_type must be one of {', '.join(AI_USAGE_COST_TYPES)}", "cost_type")
        requested_period = period
        period = coarsen_period(date_from, date_to, period)

        if project is None:
            if not is_global_admin(viewer):
                raise PermissionDeniedError("Only administrators can see the instance AI spend report")
        else:
            await ProjectService().require_project_access(session, project, viewer)

        rates = ExchangeRateService()
        if isinstance(rate_policy, RatePolicy):
            policy = rate_policy
        else:
            policy = parse_policy(rate_policy, (await rates.get_settings(session)).max_age_days)
        if currency:
            target = (await CurrencyService().require_selectable(session, currency, field="currency")).code
        else:
            target = await CurrencyService().reporting_currency_for(session, project)

        # Who may see seat cost (see the module docstring).
        if project is None:
            seats_allowed = True
        else:
            seats_allowed = await self.sees_every_entry(session, viewer, project, date_from, date_to)
        seat_shown = include_seat_cost and seats_allowed
        unallocated_shown = seat_shown and project is None and not filters.excludes_seats

        conditions = await self._usage._list_conditions(
            session,
            viewer,
            project=project,
            user_id=filters.user_id,
            date_from=date_from,
            date_to=date_to,
            tool=filters.tool,
            model=filters.model,
            provider=filters.provider,
            cost_type=filters.cost_type,
            excluded=False,
        )
        raw = await self._grouped_rows(session, conditions, dims)
        visible_issues = await self._visible_issues(session, viewer, {r["issue"] for r in raw if r["issue"]})
        builder = _RowBuilder(dims, period)
        for r in raw:
            if "issue" in dims and r["issue"] is not None and r["issue"] not in visible_issues:
                r["issue"] = HIDDEN_ISSUE
            builder.add_usage(r)

        seat_items: list[tuple[ReportRow, Money, date, bool]] = []
        if seat_shown:
            seat_items = await self._seat_items(
                session,
                builder,
                raw,
                date_from=date_from,
                date_to=date_to,
                user_id=filters.user_id,
                unallocated=unallocated_shown,
            )

        await self._convert(session, rates, builder, seat_items, target, policy)
        rows = builder.rows()
        totals = _totals_row(rows)
        for row in [*rows, totals]:
            _finish(row, target, seat_shown)
        await self._label(session, viewer, rows, dims)
        rows.sort(key=_sort_key(dims, period, sort))
        missing = await unpriced_models(session, conditions) if totals.unpriced_entries else []
        return SpendReport(
            scope="instance" if project is None else "project",
            project=project,
            date_from=date_from,
            date_to=date_to,
            group_by=dims,
            period=period,
            currency=target,
            rate_policy=policy,
            include_seat_cost=include_seat_cost,
            seat_cost_shown=seat_shown,
            seat_cost_hidden=include_seat_cost and not seats_allowed,
            unallocated_shown=unallocated_shown,
            sort=sort,
            filters=filters,
            rows=rows,
            totals=totals,
            unpriced_models=missing,
            requested_period=requested_period if requested_period != period else None,
        )

    async def sees_every_entry(
        self, session: AsyncSession, viewer: User, project: Project, date_from: date, date_to: date
    ) -> bool:
        """Whether *viewer* reads every counted entry of *project* from *date_from* to *date_to*.

        Administrators do. Anyone else needs ``view_ai_usage`` with visibility
        ``all`` there, and no counted entry of the range may be hidden from
        them (entries on private issues they cannot see). The gate of seat
        cost in project reports.
        """
        from specivo.services.api_key_scope import admin_reaches
        from specivo.services.permission_service import ai_usage_visibility

        if admin_reaches(viewer, project.id):
            return True
        if viewer.is_anonymous or await ai_usage_visibility(session, viewer, project) != AI_USAGE_VISIBILITY_ALL:
            return False
        clause = await self._usage.visible_entries_clause(session, viewer, project)
        if clause is None:
            return True
        hidden = await session.scalar(
            select(
                exists().where(
                    AiUsageEntry.project_id == project.id,
                    AiUsageEntry.usage_date.between(date_from, date_to),
                    AiUsageEntry.excluded_at.is_(None),
                    ~clause,
                )
            )
        )
        return not hidden

    # -- the grouped query ---------------------------------------------------------

    @staticmethod
    async def _grouped_rows(
        session: AsyncSession, conditions: list[Any], dims: tuple[str, ...]
    ) -> list[dict[str, Any]]:
        """One row per group x day x user x subscription x currencies, with the sums.

        The day, user, subscription and cost type are always in the grouping:
        conversion goes by day and allocation by user, month and subscription.
        """
        keys = {
            "usage_date": AiUsageEntry.usage_date,
            "user": AiUsageEntry.user_id,
            "subscription_id": AiUsageEntry.subscription_id,
            "cost_type": AiUsageEntry.cost_type,
            "marginal_currency": AiUsageEntry.marginal_currency,
            "api_currency": AiUsageEntry.api_equivalent_currency,
        }
        for dim in dims:
            keys.setdefault(dim, _COLUMNS[dim])
        names = list(keys)
        columns = [keys[n].label(f"k_{n}") for n in names]
        aggregates = [
            func.count().label("entries"),
            func.count().filter(AiUsageEntry.cost_basis == "unpriced").label("unpriced"),
            func.coalesce(func.sum(AiUsageEntry.request_count), 0).label("requests"),
            *[func.coalesce(func.sum(AiUsageEntry.class_total(c)), 0).label(f"t_{c}") for c in TOKEN_CLASSES],
            func.sum(AiUsageEntry.marginal_amount).label("marginal"),
            func.sum(AiUsageEntry.api_equivalent_amount).label("api"),
        ]
        stmt = select(*columns, *aggregates).where(*conditions).group_by(*[keys[n] for n in names])
        out: list[dict[str, Any]] = []
        for row in (await session.execute(stmt)).mappings():
            item = {n: row[f"k_{n}"] for n in names}
            item.update(
                entries=int(row["entries"]),
                unpriced=int(row["unpriced"]),
                requests=int(row["requests"]),
                tokens={c: int(row[f"t_{c}"]) for c in TOKEN_CLASSES},
                marginal=row["marginal"],
                api=row["api"],
            )
            for dim in GROUP_BY:
                item.setdefault(dim, None)
            out.append(item)
        return out

    @staticmethod
    async def _visible_issues(session: AsyncSession, viewer: User, issue_ids: set[int]) -> set[int]:
        from specivo.services.issue_service import IssueService

        if not issue_ids:
            return set()
        stmt = select(Issue.id).where(Issue.id.in_(issue_ids))
        clause = await IssueService().visible_issues_clause(session, viewer)
        if clause is not None:
            stmt = stmt.where(clause)
        return set((await session.execute(stmt)).scalars().all())

    # -- seat allocation -------------------------------------------------------------

    async def _seat_items(
        self,
        session: AsyncSession,
        builder: _RowBuilder,
        raw: list[dict[str, Any]],
        *,
        date_from: date,
        date_to: date,
        user_id: int | None,
        unallocated: bool,
    ) -> list[tuple[ReportRow, Money, date, bool]]:
        """The seat cost shares as ``(row, amount in the plan's currency, rate day, allocated?)``."""
        first, last = month_first(date_from.year, date_from.month), month_last(date_to.year, date_to.month)
        stmt = select(AiSubscription).where(
            AiSubscription.starts_on <= last,
            (AiSubscription.ends_on.is_(None)) | (AiSubscription.ends_on >= first),
        )
        if unallocated:
            if user_id is not None:
                stmt = stmt.where(AiSubscription.user_id == user_id)
        else:
            used = {r["subscription_id"] for r in raw if r["subscription_id"] is not None}
            if not used:
                return []
            stmt = stmt.where(AiSubscription.id.in_(used))
        subscriptions = {s.id: s for s in (await session.execute(stmt)).scalars().all()}
        if not subscriptions:
            return []

        months = list(months_between(date_from, date_to))
        # The month's whole prorated fee, per subscription and month.
        fees: dict[tuple[int, int, int], Decimal] = {}
        for sub in subscriptions.values():
            for year, month in months:
                fee, days = prorated_fee(sub.amount, sub.billing_period, sub.starts_on, sub.ends_on, year, month)
                if days:
                    fees[(sub.id, year, month)] = fee

        weights, factors = await self._weights(session, subscriptions, first, last)
        today = utcnow().date()
        items: list[tuple[ReportRow, Money, date, bool]] = []

        def rate_day(year: int, month: int) -> date:
            return min(month_last(year, month), today) if SEAT_RATE_DAY == "month_end" else month_first(year, month)

        # Allocated shares, on the rows of the entries that carry them.
        for r in raw:
            sub_id = r["subscription_id"]
            if r["cost_type"] != "subscription" or sub_id is None or r["api"] is None or r["api_currency"] is None:
                continue
            holder = subscriptions.get(sub_id)
            day: date = r["usage_date"]
            if holder is None or (holder.id, day.year, day.month) not in fees:
                continue
            total = weights.get((holder.id, day.year, day.month))
            factor = factors.get((r["api_currency"], holder.currency_code, day))
            if not total or factor is None:
                continue
            weight = r["api"] * factor
            share = (fees[(holder.id, day.year, day.month)] * weight / total).quantize(
                _SHARE_EXPONENT, rounding=ROUND_HALF_UP
            )
            items.append((builder.row_for(r), Money(share, holder.currency_code), rate_day(day.year, day.month), True))

        # Fees no entry carries.
        if unallocated:
            for (sub_id, year, month), _fee in sorted(fees.items()):
                if weights.get((sub_id, year, month)):
                    continue
                sub = subscriptions[sub_id]
                part, days = prorated_fee(
                    sub.amount,
                    sub.billing_period,
                    sub.starts_on,
                    sub.ends_on,
                    year,
                    month,
                    first=date_from,
                    last=date_to,
                )
                if not days or not part:
                    continue
                day = max(month_first(year, month), date_from, sub.starts_on)
                row = builder.seat_row(user_id=sub.user_id, provider=sub.provider, subscription_id=sub.id, day=day)
                amount = Money(part.quantize(_SHARE_EXPONENT, rounding=ROUND_HALF_UP), sub.currency_code)
                items.append((row, amount, rate_day(year, month), False))
        return items

    async def _weights(
        self,
        session: AsyncSession,
        subscriptions: dict[int, AiSubscription],
        first: date,
        last: date,
    ) -> tuple[dict[tuple[int, int, int], Decimal], dict[tuple[str, str, date], Decimal]]:
        """Allocation weights per subscription and month, and the factors converting values into plan currencies.

        Every counted ``subscription`` entry linked to the plans, whoever may
        see it. A value that cannot be converted into the plan's currency
        weighs nothing, like an unpriced entry. Returns ``(weights, factors)``
        where ``factors[(from, to, day)]`` is the effective rate.
        """
        from specivo.services.exchange_rate_service import ExchangeRateService

        stmt = (
            select(
                AiUsageEntry.subscription_id,
                AiUsageEntry.usage_date,
                AiUsageEntry.api_equivalent_currency,
                func.sum(AiUsageEntry.api_equivalent_amount),
            )
            .where(
                AiUsageEntry.subscription_id.in_(list(subscriptions)),
                AiUsageEntry.cost_type == "subscription",
                AiUsageEntry.excluded_at.is_(None),
                AiUsageEntry.api_equivalent_amount.is_not(None),
                AiUsageEntry.usage_date.between(first, last),
            )
            .group_by(AiUsageEntry.subscription_id, AiUsageEntry.usage_date, AiUsageEntry.api_equivalent_currency)
        )
        values: list[tuple[int, date, str, Decimal]] = [
            (sid, day, str(code), amount)
            for sid, day, code, amount in (await session.execute(stmt)).all()
            if sid is not None and amount is not None
        ]
        factors: dict[tuple[str, str, date], Decimal] = {}
        # Values in another currency than their plan's, converted once per plan currency at the entry date.
        foreign: dict[str, list[tuple[Money, date]]] = defaultdict(list)
        for sid, day, code, amount in values:
            target = subscriptions[sid].currency_code
            if code == target:
                factors[(code, target, day)] = Decimal(1)
            else:
                foreign[target].append((Money(amount, code), day))
        rates = ExchangeRateService()
        # A weight is converted at the rate of its own day, whatever policy the report uses.
        for target, items in foreign.items():
            for result in await rates.convert_many(session, items, target):
                if isinstance(result, Converted):
                    factors[(result.original.currency, target, result.on)] = result.rate.rate
        weights: dict[tuple[int, int, int], Decimal] = defaultdict(Decimal)
        for sid, day, code, amount in values:
            factor = factors.get((code, subscriptions[sid].currency_code, day))
            if factor is not None:
                weights[(sid, day.year, day.month)] += amount * factor
        return dict(weights), factors

    # -- conversion and labels ---------------------------------------------------------

    @staticmethod
    async def _convert(
        session: AsyncSession,
        rates: Any,
        builder: _RowBuilder,
        seat_items: list[tuple[ReportRow, Money, date, bool]],
        target: str,
        policy: RatePolicy,
    ) -> None:
        """Convert every amount in one batch (two queries) and file the results on their rows."""
        targets: list[tuple[ReportRow, str]] = []
        items: list[tuple[Money, date]] = []
        for row, measure, money, day in builder.amounts():
            targets.append((row, measure))
            items.append((money, day))
        for row, money, day, allocated in seat_items:
            targets.append((row, "seat_allocated" if allocated else "seat_unallocated"))
            items.append((money, day))
        if not items:
            return
        results = await rates.convert_many(session, items, target, policy)
        for (row, measure), result in zip(targets, results, strict=True):
            row._results[measure].append(result)

    @staticmethod
    async def _label(session: AsyncSession, viewer: User, rows: list[ReportRow], dims: tuple[str, ...]) -> None:
        """Fill ``labels``: user names, project keys, issue keys, tool names (one query each)."""
        from specivo.services.ai_subscription_service import AiToolService

        user_ids = {r.key.get("user") for r in rows if r.key.get("user") is not None}
        project_ids = {r.key.get("project") for r in rows if r.key.get("project") is not None}
        issue_ids = {r.key.get("issue") for r in rows if r.key.get("issue")}
        users: dict[int, str] = {}
        if "user" in dims and user_ids:
            for u in (await session.execute(select(User).where(User.id.in_(user_ids)))).scalars():
                users[u.id] = u.display_name or u.login
        projects: dict[int, str] = {}
        if "project" in dims and project_ids:
            stmt = select(Project.id, Project.key).where(Project.id.in_(project_ids))
            projects = {pid: key for pid, key in (await session.execute(stmt)).all()}
        issues: dict[int, str] = {}
        if "issue" in dims and issue_ids:
            stmt_i = select(Issue.id, Issue.project_key, Issue.sequence_number).where(Issue.id.in_(issue_ids))
            issues = {iid: f"{key}-{n}" for iid, key, n in (await session.execute(stmt_i)).all()}
        tools: dict[str, str] = {}
        if "tool" in dims:
            tools = {t.slug: t.name for t in await AiToolService().list_tools(session, active_only=False)}
        for row in rows:
            for dim in dims:
                value = row.key.get(dim)
                if value is None:
                    row.labels[dim] = ""
                elif dim == "user":
                    row.labels[dim] = users.get(value, str(value))
                elif dim == "project":
                    row.labels[dim] = projects.get(value, "")
                elif dim == "issue":
                    row.labels[dim] = issues.get(value, "") if value != HIDDEN_ISSUE else ""
                elif dim == "tool":
                    row.labels[dim] = tools.get(value, value)
                else:
                    row.labels[dim] = str(value)


# ---------------------------------------------------------------------------
# Building rows
# ---------------------------------------------------------------------------


class _RowBuilder:
    """Folds the grouped query rows into report rows and collects the amounts to convert."""

    def __init__(self, dims: tuple[str, ...], period: str | None) -> None:
        self.dims = dims
        self.period = period
        self._rows: dict[tuple[Any, ...], ReportRow] = {}
        # Per row and measure: (currency, day) -> sum, so each day and currency is converted once.
        self._amounts: dict[tuple[Any, ...], dict[str, dict[tuple[str, date], Decimal]]] = {}
        self._seat_only = any(d not in _SEAT_DIMENSIONS for d in dims)

    def _get(self, key_values: dict[str, Any], day: date, seat_only: bool = False) -> tuple[tuple[Any, ...], ReportRow]:
        start = period_start(day, self.period)
        key = (seat_only, start, *[key_values.get(d) for d in self.dims])
        row = self._rows.get(key)
        if row is None:
            row = ReportRow(key={d: key_values.get(d) for d in self.dims}, period=start, seat_only=seat_only)
            self._rows[key] = row
            self._amounts[key] = {"marginal": defaultdict(Decimal), "api_equivalent": defaultdict(Decimal)}
        return key, row

    def row_for(self, raw: dict[str, Any]) -> ReportRow:
        return self._get(raw, raw["usage_date"])[1]

    def add_usage(self, raw: dict[str, Any]) -> None:
        key, row = self._get(raw, raw["usage_date"])
        row.entries += raw["entries"]
        row.unpriced_entries += raw["unpriced"]
        row.request_count += raw["requests"]
        for c, n in raw["tokens"].items():
            row.tokens[c] += n
        day = raw["usage_date"]
        if raw["marginal"] is not None and raw["marginal_currency"]:
            self._amounts[key]["marginal"][(raw["marginal_currency"], day)] += raw["marginal"]
        if raw["api"] is not None and raw["api_currency"]:
            self._amounts[key]["api_equivalent"][(raw["api_currency"], day)] += raw["api"]
            if raw["cost_type"] == "subscription":
                self._amounts[key].setdefault("seat_api", defaultdict(Decimal))
                self._amounts[key]["seat_api"][(raw["api_currency"], day)] += raw["api"]

    def seat_row(self, *, user_id: int, provider: str, subscription_id: int, day: date) -> ReportRow:
        values = {"user": user_id, "provider": provider, "cost_type": "subscription", "subscription": subscription_id}
        return self._get(values, day, seat_only=self._seat_only)[1]

    def amounts(self) -> Iterable[tuple[ReportRow, str, Money, date]]:
        for key, measures in self._amounts.items():
            row = self._rows[key]
            for measure, sums in measures.items():
                for (code, day), amount in sorted(sums.items()):
                    yield row, measure, Money(amount, code), day

    def rows(self) -> list[ReportRow]:
        return list(self._rows.values())


def _totals_row(rows: list[ReportRow]) -> ReportRow:
    total = ReportRow(key={}, period=None)
    for row in rows:
        total.entries += row.entries
        total.unpriced_entries += row.unpriced_entries
        total.request_count += row.request_count
        for c, n in row.tokens.items():
            total.tokens[c] += n
        for measure, results in row._results.items():
            total._results[measure].extend(results)
    return total


def _finish(row: ReportRow, currency: str, seat_shown: bool) -> None:
    """Fold the row's conversion results into summaries, total spend and leverage."""
    res = row._results
    row.marginal = summarize(res.get("marginal", []), currency)
    row.api_equivalent = summarize(res.get("api_equivalent", []), currency)
    if seat_shown:
        row.seat_allocated = summarize(res.get("seat_allocated", []), currency)
        row.seat_unallocated = summarize(res.get("seat_unallocated", []), currency)
        seats = [*res.get("seat_allocated", []), *res.get("seat_unallocated", [])]
        row.total_spend = summarize([*res.get("marginal", []), *seats], currency)
        seat_total = summarize(seats, currency).total
        row.seat_api = summarize(res.get("seat_api", []), currency)
        if seat_total.amount > 0:
            covered = row.seat_api.total
            row.leverage = (covered.amount / seat_total.amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    else:
        row.total_spend = row.marginal


def _sort_key(dims: tuple[str, ...], period: str | None, sort: str) -> Any:
    def key(row: ReportRow) -> tuple[Any, ...]:
        parts: list[Any] = []
        if sort == "spend":
            spend = row.total_spend.total.amount if row.total_spend is not None else Decimal(0)
            parts.append(-spend)
        parts.append(row.period or date.min)
        parts.append(row.seat_only)
        for dim in dims:
            label = row.labels.get(dim, "")
            # Empty values ("no project", "no branch") last.
            parts.append((label == "", label.lower()))
        return tuple(parts)

    return key


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------

CSV_TOKEN_COLUMNS = tuple(f"tokens_{c}" for c in TOKEN_CLASSES)


def _amount(summary: ConversionSummary | None) -> str:
    if summary is None:
        return ""
    return format_decimal_plain(summary.total.quantize().amount)


def _unconverted(name: str, summary: ConversionSummary | None) -> str:
    if summary is None:
        return ""
    return "; ".join(
        f"{name}: {format_decimal_plain(g.amount.quantize().amount)} {g.currency} ({g.reason})"
        for g in summary.unconverted
    )


def report_csv(report: SpendReport) -> str:
    """The report as CSV: one line per row, then a ``total`` line. Amounts in the reporting currency.

    Values a formula could start with are quoted with a leading apostrophe
    (labels come from people: branch and model names).
    """
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    seat = report.seat_cost_shown
    header = ["period", *[f"{d}" for d in report.group_by], "entries", "requests", *CSV_TOKEN_COLUMNS]
    header += ["currency", "marginal_spend", "api_equivalent_value"]
    if seat:
        header += ["allocated_seat_cost", "unallocated_seat_cost", "total_spend", "leverage"]
    header += ["unpriced_entries", "not_converted"]
    writer.writerow(header)

    def line(row: ReportRow, label: str | None) -> list[str]:
        cells = [row.period.isoformat() if row.period else (label or "")]
        for dim in report.group_by:
            value = row.labels.get(dim, "")
            if dim == "issue" and row.key.get("issue") == HIDDEN_ISSUE:
                value = "(hidden)"
            elif row.seat_only and row.key.get(dim) is None:
                value = "(unallocated seat)"
            cells.append(_safe(value))
        cells += [str(row.entries), str(row.request_count), *[str(row.tokens[c]) for c in TOKEN_CLASSES]]
        cells += [report.currency, _amount(row.marginal), _amount(row.api_equivalent)]
        if seat:
            cells += [
                _amount(row.seat_allocated),
                _amount(row.seat_unallocated),
                _amount(row.total_spend),
                format_decimal_plain(row.leverage) if row.leverage is not None else "",
            ]
        notes = "; ".join(
            n
            for n in (
                _unconverted("marginal_spend", row.marginal),
                _unconverted("api_equivalent_value", row.api_equivalent),
                _unconverted("allocated_seat_cost", row.seat_allocated) if seat else "",
                _unconverted("unallocated_seat_cost", row.seat_unallocated) if seat else "",
            )
            if n
        )
        cells += [str(row.unpriced_entries), notes]
        return cells

    for row in report.rows:
        writer.writerow(line(row, None))
    writer.writerow(line(report.totals, "total"))
    return buf.getvalue()


def _safe(value: str) -> str:
    """Neutralise spreadsheet formulas in a cell (``=``, ``+``, ``-``, ``@`` and control starts)."""
    if value and value[0] in "=+-@\t\r":
        return "'" + value
    return value


def default_range(preset: str | None, today: date | None = None) -> tuple[date, date]:
    """``(first, last)`` of a period preset: ``7d``, ``30d`` (default), ``this_month``, ``last_month``."""
    today = today or utcnow().date()
    if preset == "7d":
        return today - timedelta(days=6), today
    if preset == "this_month":
        return today.replace(day=1), today
    if preset == "last_month":
        last = today.replace(day=1) - timedelta(days=1)
        return last.replace(day=1), last
    return today - timedelta(days=29), today


PRESETS: tuple[str, ...] = ("7d", "30d", "this_month", "last_month", "custom")


def normalize_filter_provider(value: str | None) -> str | None:
    if not value or not value.strip():
        return None
    return normalize_provider(value) or value.strip().lower()


__all__ = [
    "GROUP_BY",
    "HIDDEN_ISSUE",
    "PERIODS",
    "PRESETS",
    "SEAT_RATE_DAY",
    "SORTS",
    "AiSpendReportService",
    "PolicyKind",
    "ReportFilters",
    "ReportRow",
    "SpendReport",
    "default_range",
    "parse_group_by",
    "period_end",
    "period_start",
    "report_csv",
]
