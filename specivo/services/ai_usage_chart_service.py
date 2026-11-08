"""Chart data for AI usage: value and tokens over time, where the value went, and subscription leverage.

Three scopes, one implementation:

- ``own`` (:meth:`AiUsageChartService.charts`, the "My AI usage" page): the
  reader's own entries, with the page's filters;
- ``project`` (:meth:`AiUsageChartService.report_charts` with a project, the
  project spend report): the project's entries the reader may read;
- ``instance`` (:meth:`AiUsageChartService.report_charts` without one, the
  admin "AI spend" page): every entry, administrators only (403 otherwise).

Each returns plain numbers:

- **over time**: the API-equivalent value per time bucket, split by tool and by
  model (on the spend reports also by user and by project; the page switches
  between them), the spend per bucket, and the number of unpriced entries per
  bucket, so a bucket that looks cheap because its model has no price can say
  so; on the spend reports, the seat cost per bucket where the reader may see it;
- **tokens over time**: the token classes per bucket (both token buckets,
  base and long-context, summed per class);
- **breakdown**: the API-equivalent value and the spend by project and by
  model (on the spend reports also by user and by tool), the largest
  :data:`TOP_N` plus one "other" group;
- **leverage**: on My AI usage, per subscription plan of the reader, the
  API-equivalent value of the usage it covered over the plan's fee for the
  period, when the filters leave the whole plan in view. On the spend reports,
  where the reader may see seat cost: the seat cost the report counts per plan
  (instance only: plan names are private to their holder and administrators),
  per person and in total, against the value of the subscription usage. A
  project report counts only the shares the project's entries carry. Each of
  those tables lists the :data:`LEVERAGE_TOP` largest by fee, then by value,
  and sums the rest into one "others" row (exact sums, its own ratio).

Visibility and filters are those of the page the charts sit on
(``AiUsageService._list_conditions``): on My AI usage counted entries by
default, or the excluded ones when the page shows only those; on the spend
reports the counted entries the report counts, with the report's rules on who
sees seat cost (:class:`AiSpendReportService`, whose
:meth:`~AiSpendReportService.seat_breakdown` gives the seat numbers, so the
charts add up to the report table). Amounts are converted into the reporting
currency at each entry's day (My AI usage: the default rate policy; the spend
reports: the report's); amounts without a usable rate are left out of the
numbers and counted in ``unconverted`` (the totals box above lists them).

Buckets are UTC days for a range of up to :data:`DAY_MAX_DAYS` days, ISO
weeks (from Monday) up to :data:`WEEK_MAX_DAYS` days, then calendar months,
quarters or years, the first that keeps to :data:`AUTO_MAX_BUCKETS` points;
the reader may pick days, weeks or months instead (up to :data:`MAX_BUCKETS`).
The range runs from the first to the last day with a matching entry (the
page's dates only filter the entries; on the instance report it also covers
the days of plans whose fees nobody's usage carried, which the report counts),
and every bucket in it is listed, empty ones too, so the time axis is even.
Entries spanning more than :data:`MAX_BUCKETS` years are not charted
(``too_long``).

Query count: one min/max, one grouped query for the entries, the label lookups
(tools, projects, users on the spend reports), one or two conversions (two
queries each), and the subscriptions (My AI usage) or the seat breakdown (the
spend reports: a bounded report of its own).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import TIMESTAMP, Date, and_, case, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.ai_pricing import TOKEN_CLASSES
from specivo.core.ai_subscriptions import month_last, months_between, prorated_fee
from specivo.core.exceptions import PermissionDeniedError
from specivo.core.exchange import Converted, RatePolicy
from specivo.core.money import Money
from specivo.core.utils import utcnow
from specivo.models.ai_subscription import AiSubscription
from specivo.models.ai_usage_entry import AiUsageEntry
from specivo.models.project import Project
from specivo.models.user import User
from specivo.services.ai_usage_service import AiUsageService

if TYPE_CHECKING:
    from specivo.services.ai_spend_report_service import AiSpendReportService, ReportFilters

#: Bucket sizes, finest first.
BUCKET_SIZES: tuple[str, ...] = ("day", "week", "month", "quarter", "year")
#: Sizes the reader may pick instead of the automatic one.
CHOSEN_SIZES: tuple[str, ...] = ("day", "week", "month")
#: The automatic size: days up to DAY_MAX_DAYS, weeks up to WEEK_MAX_DAYS, then
#: months, quarters or years, whichever first keeps to AUTO_MAX_BUCKETS points.
DAY_MAX_DAYS = 62
WEEK_MAX_DAYS = 371
AUTO_MAX_BUCKETS = 120
#: Most buckets a chart draws, whatever is asked: a chosen size past it falls
#: back to the automatic one, and entries spanning more years than that are
#: not charted.
MAX_BUCKETS = AUTO_MAX_BUCKETS
#: Groups shown by name in a breakdown or a stack; the rest are summed as one "other" group.
TOP_N = 8
#: Groups a breakdown keeps by name, so a page can show more than ``TOP_N`` (the spend reports'
#: per-chart choice, :data:`BREAKDOWN_SHOWS`) and fold the rest itself.
BREAKDOWN_MAX = 100
#: How many groups a spend report's breakdown chart may show by name; the first is the default.
BREAKDOWN_SHOWS = (TOP_N, 25, 50, BREAKDOWN_MAX)
#: Plans and people a leverage table of the spend reports names; the rest are summed into one row.
LEVERAGE_TOP = 10
#: Key of the "other" group (the page translates it).
OTHER = "\x00other"
#: Key of entries without a project (the page translates it).
NO_PROJECT = "\x00none"


#: Series colours of the theme (``--sp-chart-1`` .. ``--sp-chart-8``); the last, a grey, is "other".
COLOR_SLOTS = 8
#: The slot of "other", "none" and every group past the first seven.
NEUTRAL_SLOT = COLOR_SLOTS - 1


def color_slots(keys: Iterable[Any]) -> dict[Any, int]:
    """The colour of each group, by rank: a slot (0-based) of the theme's chart palette.

    *keys* come largest first (as the chart stacks do). The first seven get
    a colour each, the rest and ``OTHER`` / ``NO_PROJECT`` / empty keys the
    neutral grey. The spend report pages colour their tables, bars and person
    cards with the slots of the chart on the same page, so a person or tool
    has the same colour everywhere on it, and no two of the largest share one.
    """
    out: dict[Any, int] = {}
    rank = 0
    for key in keys:
        if key in out:
            continue
        if key is None or key == "" or key == OTHER or key == NO_PROJECT or rank >= NEUTRAL_SLOT:
            out[key] = NEUTRAL_SLOT
        else:
            out[key] = rank
            rank += 1
    return out


def bucket_start(day: date, size: str) -> date:
    """The first day of the *size* bucket holding *day* (weeks start on Monday)."""
    if size == "day":
        return day
    if size == "week":
        return day - timedelta(days=day.weekday())
    if size == "month":
        return day.replace(day=1)
    if size == "quarter":
        return date(day.year, (day.month - 1) // 3 * 3 + 1, 1)
    return date(day.year, 1, 1)


def bucket_count(first: date, last: date, size: str) -> int:
    """How many *size* buckets the range from *first* to *last* spans (computed, nothing built)."""
    if size == "day":
        return (last - first).days + 1
    if size == "week":
        return (last - first + timedelta(days=first.weekday())).days // 7 + 1
    if size == "month":
        return (last.year - first.year) * 12 + last.month - first.month + 1
    if size == "quarter":
        return (last.year - first.year) * 4 + (last.month - 1) // 3 - (first.month - 1) // 3 + 1
    return last.year - first.year + 1


def bucket_size(first: date, last: date, chosen: str | None = None) -> str:
    """The bucket for a range from *first* to *last* (inclusive): *chosen* when it is
    one of :data:`CHOSEN_SIZES` and stays within :data:`MAX_BUCKETS`, else the automatic one."""
    if chosen in CHOSEN_SIZES and bucket_count(first, last, chosen) <= MAX_BUCKETS:
        return chosen
    days = (last - first).days + 1
    if days <= DAY_MAX_DAYS:
        return "day"
    if days <= WEEK_MAX_DAYS:
        return "week"
    for size in ("month", "quarter"):
        if bucket_count(first, last, size) <= AUTO_MAX_BUCKETS:
            return size
    return "year"


def bucket_starts(first: date, last: date, size: str) -> list[date]:
    """The first day of every *size* bucket from *first*'s bucket to *last*'s, in order.

    Steps only while a later bucket remains, so a range ending near
    ``date.max`` never steps past it.
    """
    start, end = bucket_start(first, size), bucket_start(last, size)
    months = {"month": 1, "quarter": 3, "year": 12}.get(size, 0)
    out: list[date] = []
    while True:
        out.append(start)
        if start >= end:
            break
        if size == "day":
            start += timedelta(days=1)
        elif size == "week":
            start += timedelta(days=7)
        else:
            index = start.year * 12 + start.month - 1 + months
            start = date(index // 12, index % 12 + 1, 1)
    return out


def top_groups(totals: dict[str, Decimal], n: int = TOP_N) -> list[str]:
    """The keys of the *n* largest *totals*, largest first (ties by key).

    All of them when there is at most one more: an "other" group of one is no help.
    """
    ordered = sorted(totals, key=lambda k: (-totals[k], k))
    if len(ordered) <= n + 1:
        return ordered
    return ordered[:n]


@dataclass
class Bucket:
    """One time bucket: its first day and what its entries add up to."""

    start: date
    entries: int = 0
    unpriced: int = 0
    #: Amounts that could not be converted into the reporting currency (left out of the numbers).
    unconverted: int = 0
    spend: Decimal = Decimal(0)
    api: Decimal = Decimal(0)
    #: Seat cost carried by the bucket (report scopes, when the reader may see seat cost).
    seat: Decimal = Decimal(0)
    api_by_tool: dict[str, Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    api_by_model: dict[str, Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    api_by_user: dict[str, Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    api_by_project: dict[str, Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    tokens: dict[str, int] = field(default_factory=lambda: dict.fromkeys(TOKEN_CLASSES, 0))


@dataclass
class Share:
    """One group of a breakdown: its key, label and amounts."""

    key: str
    label: str
    api: Decimal = Decimal(0)
    spend: Decimal = Decimal(0)
    entries: int = 0
    #: How many groups the share stands for: more than one only for ``OTHER``.
    groups: int = 1


@dataclass
class PlanLeverage:
    """One subscription over the period: the fee, the value of the usage it covered, the ratio.

    On My AI usage the fee is the plan's prorated fee for the period; on the
    spend reports it is the seat cost the report counts for the plan
    (allocated shares, plus unallocated fees on the instance report).
    """

    subscription_id: int
    plan_name: str
    fee: Decimal | None
    api: Decimal
    #: ``api / fee`` (two decimals); None without a fee in the period or a fee that does not convert.
    leverage: Decimal | None
    #: The plan's holder (spend reports only).
    user_id: int | None = None
    holder: str = ""


@dataclass
class PersonLeverage:
    """One person's subscriptions together (or every subscription, for the total): fees, value covered, ratio."""

    user_id: int | None
    label: str
    fee: Decimal | None
    api: Decimal
    leverage: Decimal | None


@dataclass
class UsageCharts:
    """What the charts draw; amounts in ``currency``."""

    currency: str
    #: One of :data:`SCOPES`.
    scope: str = "own"
    bucket: str = "day"
    first: date | None = None
    last: date | None = None
    buckets: list[Bucket] = field(default_factory=list)
    #: Stack keys of the time chart, largest first (``OTHER`` last when present).
    tools: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    #: Users (ids as text) and projects (ids as text, ``NO_PROJECT``): report scopes only.
    users: list[str] = field(default_factory=list)
    projects: list[str] = field(default_factory=list)
    #: Tool names by slug (models are their own labels).
    tool_labels: dict[str, str] = field(default_factory=dict)
    #: User names and project keys by stack key (report scopes only).
    user_labels: dict[str, str] = field(default_factory=dict)
    project_labels: dict[str, str] = field(default_factory=dict)
    by_project: list[Share] = field(default_factory=list)
    by_model: list[Share] = field(default_factory=list)
    #: Report scopes only.
    by_user: list[Share] = field(default_factory=list)
    by_tool: list[Share] = field(default_factory=list)
    #: Per plan. My AI usage: the reader's plans; instance report: every plan; project report: none
    #: (plan details stay with their holder and administrators).
    leverage: list[PlanLeverage] = field(default_factory=list)
    #: Spend reports: the plans past the first LEVERAGE_TOP, summed (``plan_name`` empty), and how many.
    leverage_others: PlanLeverage | None = None
    leverage_others_count: int = 0
    #: Spend reports: per person (the first LEVERAGE_TOP), and all of them together (None without seat cost).
    people: list[PersonLeverage] = field(default_factory=list)
    #: The people past the first LEVERAGE_TOP, summed, and how many.
    people_others: PersonLeverage | None = None
    people_others_count: int = 0
    leverage_total: PersonLeverage | None = None
    #: Whether seat cost is part of the numbers (spend reports, readers who may see it).
    seat_shown: bool = False
    entries: int = 0
    unpriced: int = 0
    unconverted: int = 0
    #: The entries span more than MAX_BUCKETS months: nothing is charted.
    too_long: bool = False

    @property
    def empty(self) -> bool:
        return self.entries == 0

    @property
    def has_spend(self) -> bool:
        return any(b.spend or b.seat for b in self.buckets)

    @property
    def has_seat(self) -> bool:
        return self.seat_shown and any(b.seat for b in self.buckets)


@dataclass
class _Fees:
    """Seat cost and covered value of one plan, person or all of them, while they are summed."""

    user_id: int | None
    fee: Decimal = Decimal(0)
    api: Decimal = Decimal(0)
    #: Some fee did not convert into the reporting currency: no fee and no ratio are shown.
    broken: bool = False

    def add(self, other: _Fees) -> None:
        self.fee += other.fee
        self.api += other.api
        self.broken = self.broken or other.broken

    def person(self) -> PersonLeverage:
        fee = None if self.broken or not self.fee else self.fee
        ratio = (self.api / fee).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if fee else None
        return PersonLeverage(user_id=self.user_id, label="", fee=fee, api=self.api, leverage=ratio)


def _top_and_rest[T](ranked: list[T]) -> tuple[list[T], list[T]]:
    """The first :data:`LEVERAGE_TOP` of *ranked* and the rest; all of them when only one would be left."""
    if len(ranked) <= LEVERAGE_TOP + 1:
        return ranked, []
    return ranked[:LEVERAGE_TOP], ranked[LEVERAGE_TOP:]


def _summed(items: Iterable[_Fees]) -> _Fees:
    """One :class:`_Fees` adding up *items* (an "others" row): exact sums, and broken if any of them is."""
    out = _Fees(user_id=None)
    for item in items:
        out.add(item)
    return out


class AiUsageChartService:
    """Build the AI usage charts (see the module docstring)."""

    def __init__(self) -> None:
        self._usage = AiUsageService()

    async def charts(
        self,
        session: AsyncSession,
        viewer: User,
        *,
        currency: str,
        project: Project | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        tool: str | None = None,
        model: str | None = None,
        cost_type: str | None = None,
        unassigned: bool | None = None,
        excluded: bool = False,
        bucket: str | None = None,
        tool_names: dict[str, str] | None = None,
    ) -> UsageCharts:
        """The charts of *viewer*'s own entries matching the filters, amounts in *currency* (scope ``own``).

        *excluded* ``True`` charts the excluded entries instead of the counted
        ones. *bucket* picks the time bucket (one of :data:`CHOSEN_SIZES`;
        automatic when left out or when it would draw too many). *tool_names*
        maps tool slugs to names (looked up when left out).
        """
        conditions = await self._usage._list_conditions(
            session,
            viewer,
            project=project,
            user_id=viewer.id,
            date_from=date_from,
            date_to=date_to,
            tool=tool,
            model=model,
            cost_type=cost_type,
            unassigned=unassigned,
            excluded=excluded,
        )
        out = UsageCharts(currency=currency)
        span = await self._span(session, conditions)
        if span is None or not self._frame(out, span[0], span[1], bucket):
            return out
        raw, converted = await self._fill(session, out, conditions, None, tool_names)
        if not excluded and not (project or unassigned or tool or model) and cost_type in (None, "subscription"):
            first, last = span
            out.leverage = await self._leverage(
                session, viewer, raw, converted, date_from or first, date_to or last, currency
            )
        return out

    async def report_charts(
        self,
        session: AsyncSession,
        viewer: User,
        *,
        project: Project | None,
        date_from: date | None,
        date_to: date | None,
        currency: str,
        rate_policy: RatePolicy | None = None,
        include_seat_cost: bool = True,
        filters: ReportFilters | None = None,
        bucket: str | None = None,
        tool_names: dict[str, str] | None = None,
        issue_id: int | None = None,
        without_issue: bool = False,
        branch: str | None = None,
        without_branch: bool = False,
        excluded: bool = False,
    ) -> UsageCharts:
        """The charts of *project*'s entries the viewer may read, or of the whole instance.

        Scope ``project`` (*project* set; the project spend report and the
        project's AI usage tab): the entries the viewer may read there, with
        the report's rules on who sees seat cost
        (:meth:`AiSpendReportService.sees_every_entry`) and no plan details
        (people only, each with the share of their fees carried by the
        project's entries). Scope ``instance`` (*project* None, the admin
        page): administrators only (403 otherwise), every plan and person,
        unallocated fees included. *currency* and *rate_policy* are the
        report's, so the charts add up to its table; the range is the days with
        entries within *date_from* to *date_to* (either may be open), with the
        days of plans whose fees nobody's usage carried on the instance report.

        The AI usage tab's own filters (*issue_id*, *without_issue*, *branch*,
        *without_branch*, *excluded* for the excluded entries instead of the
        counted ones) narrow the entries; a seat fee cannot follow them, so
        with any of them seat cost and leverage are left out, as they are over a
        range longer than a report may cover (``MAX_RANGE_DAYS``).
        """
        from specivo.services.ai_spend_report_service import MAX_RANGE_DAYS, AiSpendReportService, ReportFilters
        from specivo.services.api_key_scope import is_global_admin
        from specivo.services.project_service import ProjectService

        filters = filters or ReportFilters()
        reports = AiSpendReportService()
        if project is None:
            if not is_global_admin(viewer):
                raise PermissionDeniedError("Only administrators can see the instance AI spend report")
        else:
            await ProjectService().require_project_access(session, project, viewer)

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
            issue_id=issue_id if issue_id is not None else filters.issue_id,
            without_issue=without_issue or filters.without_issue,
            branch=branch or filters.branch,
            without_branch=without_branch or filters.without_branch,
            excluded=excluded,
        )
        # The report's own narrowing (the drill-down's project, issue and branch) follows the
        # report's rules: seat shares follow the entries (``seat_breakdown`` takes the same filters).
        conditions = [*conditions, *filters.conditions()]
        out = UsageCharts(currency=currency, scope="instance" if project is None else "project")
        span = await self._span(session, conditions)
        if span is None:
            return out
        first, last = span
        low, high = date_from or first, date_to or last
        narrowed = issue_id is not None or without_issue or bool(branch) or without_branch or excluded
        seat_shown = include_seat_cost and not narrowed and (high - low).days + 1 <= MAX_RANGE_DAYS
        if seat_shown and project is not None:
            seat_shown = await reports.sees_every_entry(session, viewer, project, low, high)
        if seat_shown and project is None and not filters.excludes_seats:
            first, last = await self._plan_span(session, first, last, low, high, filters.user_id)
        if not self._frame(out, first, last, bucket):
            return out
        out.seat_shown = seat_shown and out.bucket in CHOSEN_SIZES
        await self._fill(session, out, conditions, rate_policy, tool_names)
        if out.seat_shown:
            await self._seats(session, viewer, out, reports, project, rate_policy, filters)
        await self._label_people(session, out)
        return out

    # -- range and buckets -------------------------------------------------------------

    @staticmethod
    async def _span(session: AsyncSession, conditions: list[Any]) -> tuple[date, date] | None:
        """The first and last day with a matching entry (one indexed min/max), None without any.

        The range is the days with entries (the page's dates only filter
        them), so an open or absurd date range never widens it.
        """
        span = (
            await session.execute(
                select(func.min(AiUsageEntry.usage_date), func.max(AiUsageEntry.usage_date)).where(*conditions)
            )
        ).one()
        if span[0] is None:
            return None
        return span[0], span[1]

    @staticmethod
    async def _plan_span(
        session: AsyncSession, first: date, last: date, date_from: date, date_to: date, user_id: int | None
    ) -> tuple[date, date]:
        """*first* to *last* widened to the days within *date_from* to *date_to* that a plan runs.

        The instance report counts the fees nobody's usage carried, so the
        charts cover those days too (one min/max over the plans).
        """
        stmt = select(
            func.min(AiSubscription.starts_on), func.max(func.coalesce(AiSubscription.ends_on, date_to))
        ).where(
            AiSubscription.starts_on <= date_to,
            or_(AiSubscription.ends_on.is_(None), AiSubscription.ends_on >= date_from),
        )
        if user_id is not None:
            stmt = stmt.where(AiSubscription.user_id == user_id)
        plans = (await session.execute(stmt)).one()
        if plans[0] is None:
            return first, last
        return min(first, max(plans[0], date_from)), max(last, min(plans[1], date_to))

    @staticmethod
    def _frame(out: UsageCharts, first: date, last: date, bucket: str | None) -> bool:
        """Set the range and bucket size of *out*; False (``too_long``) when the range cannot be charted."""
        size = bucket_size(first, last, bucket)
        if bucket_count(first, last, size) > MAX_BUCKETS:
            out.too_long = True
            return False
        out.first, out.last, out.bucket = first, last, size
        return True

    # -- filling the buckets -------------------------------------------------------------

    async def _fill(
        self,
        session: AsyncSession,
        out: UsageCharts,
        conditions: list[Any],
        policy: RatePolicy | None,
        tool_names: dict[str, str] | None,
    ) -> tuple[list[dict[str, Any]], dict[tuple[int, str], Decimal]]:
        """Group the entries into *out*'s buckets, stacks and breakdowns; returns the raw rows and converted amounts."""
        from specivo.services.ai_subscription_service import AiToolService
        from specivo.services.exchange_rate_service import ExchangeRateService

        assert out.first is not None and out.last is not None
        currency = out.currency
        raw = await self._grouped(session, conditions, out.bucket, currency)
        buckets = {start: Bucket(start=start) for start in bucket_starts(out.first, out.last, out.bucket)}

        # Convert every amount at its own day (one batch, two queries).
        items: list[tuple[Money, date]] = []
        slots: list[tuple[int, str]] = []
        for i, r in enumerate(raw):
            for measure, amount, code in (
                ("spend", r["marginal"], r["marginal_currency"]),
                ("api", r["api"], r["api_currency"]),
            ):
                if amount is not None and code:
                    items.append((Money(amount, code), r["day"]))
                    slots.append((i, measure))
        converted: dict[tuple[int, str], Decimal] = {}
        unconverted_rows: set[tuple[int, str]] = set()
        if items:
            results = await ExchangeRateService().convert_many(session, items, currency, policy)
            for slot, result in zip(slots, results, strict=True):
                if isinstance(result, Converted):
                    converted[slot] = result.money.amount
                else:
                    unconverted_rows.add(slot)

        totals: dict[str, dict[str, Decimal]] = {d: defaultdict(Decimal) for d in ("tool", "model", "user", "project")}
        shares: dict[str, dict[str, Share]] = {d: {} for d in ("tool", "model", "user", "project")}
        for i, r in enumerate(raw):
            cell = buckets.get(bucket_start(r["day"], out.bucket))
            if cell is None:
                continue
            api = converted.get((i, "api"), Decimal(0))
            spend = converted.get((i, "spend"), Decimal(0))
            keys = {
                "tool": r["tool"],
                "model": r["model"] or "",
                "user": str(r["user_id"]),
                "project": str(r["project_id"]) if r["project_id"] is not None else NO_PROJECT,
            }
            cell.entries += r["entries"]
            cell.unpriced += r["unpriced"]
            cell.unconverted += sum((i, m) in unconverted_rows for m in ("api", "spend"))
            cell.api += api
            cell.spend += spend
            for dim, key in keys.items():
                getattr(cell, f"api_by_{dim}")[key] += api
                totals[dim][key] += api
                share = shares[dim].setdefault(key, Share(key=key, label=key))
                share.api += api
                share.spend += spend
                share.entries += r["entries"]
            for c, n in r["tokens"].items():
                cell.tokens[c] += n
            out.entries += r["entries"]
            out.unpriced += r["unpriced"]
        out.unconverted = len(unconverted_rows)

        out.tools = self._stack_keys(totals["tool"], buckets.values(), "api_by_tool")
        out.models = self._stack_keys(totals["model"], buckets.values(), "api_by_model")
        if out.scope != "own":
            out.users = self._stack_keys(totals["user"], buckets.values(), "api_by_user")
            out.projects = self._stack_keys(totals["project"], buckets.values(), "api_by_project")
        out.buckets = list(buckets.values())

        names = tool_names
        if names is None:
            names = {t.slug: t.name for t in await AiToolService().list_tools(session, active_only=False)}
        out.tool_labels = {k: names.get(k, k) for k in totals["tool"]}
        project_ids = [int(k) for k in shares["project"] if k != NO_PROJECT]
        project_keys: dict[int, str] = {}
        if project_ids:
            stmt = select(Project.id, Project.key).where(Project.id.in_(project_ids))
            project_keys = {pid: key for pid, key in (await session.execute(stmt)).all()}
        for key, share in shares["project"].items():
            if key != NO_PROJECT:
                share.label = project_keys.get(int(key), "")
        out.project_labels = {k: s.label for k, s in shares["project"].items()}
        out.by_project = self._top_shares(shares["project"])
        out.by_model = self._top_shares(shares["model"])
        if out.scope != "own":
            for key, share in shares["tool"].items():
                share.label = out.tool_labels.get(key, key)
            out.by_tool = self._top_shares(shares["tool"])
            out.by_user = self._top_shares(shares["user"])
        return raw, converted

    async def _label_people(self, session: AsyncSession, out: UsageCharts) -> None:
        """Name the users the stacks, breakdown and leverage tables show (one query, bounded by what they show)."""
        shown = {k for k in out.users if k != OTHER} | {s.key for s in out.by_user if s.key != OTHER}
        ids = {int(k) for k in shown if k.isdigit()} | {p.user_id for p in out.people if p.user_id is not None}
        ids |= {p.user_id for p in out.leverage if p.user_id is not None}
        if not ids:
            return
        rows = (await session.execute(select(User.id, User.display_name, User.login).where(User.id.in_(ids)))).all()
        names = {str(uid): (name or login) for uid, name, login in rows}
        out.user_labels = names
        for share in out.by_user:
            if share.key != OTHER:
                share.label = names.get(share.key, "")
        for person in out.people:
            if person.user_id is not None:
                person.label = names.get(str(person.user_id), "")
        for plan in out.leverage:
            if plan.user_id is not None:
                plan.holder = names.get(str(plan.user_id), "")

    # -- the grouped query ---------------------------------------------------------

    @staticmethod
    async def _grouped(session: AsyncSession, conditions: list[Any], size: str, currency: str) -> list[dict[str, Any]]:
        """One row per bucket x user x tool x model x project x subscription x currencies, with the sums.

        Grouped in SQL by the bucket's first day. Rows with an amount in
        another currency than *currency* keep their own day instead, since
        each day converts at its own rate; amounts already in *currency* need
        no rate, so they fold into their bucket.
        """
        day = AiUsageEntry.usage_date
        if size == "day":
            slot: Any = day
        else:
            start = func.date_trunc(size, cast(day, TIMESTAMP())).cast(Date)
            same = and_(
                or_(AiUsageEntry.marginal_currency.is_(None), AiUsageEntry.marginal_currency == currency),
                or_(AiUsageEntry.api_equivalent_currency.is_(None), AiUsageEntry.api_equivalent_currency == currency),
            )
            slot = case((same, start), else_=day)
        keys = {
            "day": slot,
            "user_id": AiUsageEntry.user_id,
            "tool": AiUsageEntry.tool_slug,
            "model": AiUsageEntry.model,
            "project_id": AiUsageEntry.project_id,
            "subscription_id": AiUsageEntry.subscription_id,
            "cost_type": AiUsageEntry.cost_type,
            "marginal_currency": AiUsageEntry.marginal_currency,
            "api_currency": AiUsageEntry.api_equivalent_currency,
        }
        columns = [col.label(f"k_{name}") for name, col in keys.items()]
        aggregates = [
            func.count().label("entries"),
            func.count().filter(AiUsageEntry.cost_basis == "unpriced").label("unpriced"),
            *[func.coalesce(func.sum(AiUsageEntry.class_total(c)), 0).label(f"t_{c}") for c in TOKEN_CLASSES],
            func.sum(AiUsageEntry.marginal_amount).label("marginal"),
            func.sum(AiUsageEntry.api_equivalent_amount).label("api"),
        ]
        stmt = select(*columns, *aggregates).where(*conditions).group_by(*[c.name for c in columns])
        out: list[dict[str, Any]] = []
        for row in (await session.execute(stmt)).mappings():
            item: dict[str, Any] = {name: row[f"k_{name}"] for name in keys}
            item.update(
                entries=int(row["entries"]),
                unpriced=int(row["unpriced"]),
                tokens={c: int(row[f"t_{c}"]) for c in TOKEN_CLASSES},
                marginal=row["marginal"],
                api=row["api"],
            )
            out.append(item)
        return out

    # -- stacks and breakdowns -------------------------------------------------------

    @staticmethod
    def _stack_keys(totals: dict[str, Decimal], buckets: Iterable[Bucket], attr: str) -> list[str]:
        """The stack keys of the time chart: the largest groups, the rest folded into ``OTHER`` in every bucket."""
        keep = top_groups(totals)
        if len(keep) == len(totals):
            return keep
        kept = set(keep)
        for bucket in buckets:
            values: dict[str, Decimal] = getattr(bucket, attr)
            other = sum((v for k, v in values.items() if k not in kept), Decimal(0))
            folded: dict[str, Decimal] = defaultdict(Decimal, {k: v for k, v in values.items() if k in kept})
            if other:
                folded[OTHER] = other
            setattr(bucket, attr, folded)
        return [*keep, OTHER]

    @staticmethod
    def _top_shares(shares: dict[str, Share]) -> list[Share]:
        """The :data:`BREAKDOWN_MAX` largest shares by API-equivalent value, the rest summed into one ``OTHER`` share.

        Pages show :data:`TOP_N` of them unless the reader asks for more
        (``fold_share_points`` in the AI usage pages).
        """
        keep = top_groups({k: s.api for k, s in shares.items()}, BREAKDOWN_MAX)
        out = [shares[k] for k in keep]
        rest = [s for k, s in shares.items() if k not in set(keep)]
        if rest:
            other = Share(key=OTHER, label=OTHER, groups=len(rest))
            for s in rest:
                other.api += s.api
                other.spend += s.spend
                other.entries += s.entries
            out.append(other)
        return out

    # -- seat cost (spend reports) -------------------------------------------------------

    @staticmethod
    async def _seats(
        session: AsyncSession,
        viewer: User,
        out: UsageCharts,
        reports: AiSpendReportService,
        project: Project | None,
        policy: RatePolicy | None,
        filters: ReportFilters,
    ) -> None:
        """Seat cost per bucket, and leverage per plan (instance only), per person and in total.

        The numbers are the spend report's (:meth:`AiSpendReportService.seat_breakdown`
        over the charted range, per bucket, user and plan), so they add up to
        the report's table. The fee of a plan is the seat cost the report
        counts for it: on a project report only the shares the project's
        entries carry, so nobody sees another person's whole fee there.
        """
        from specivo.services.ai_spend_report_service import PERIODS

        assert out.first is not None and out.last is not None
        if out.bucket not in PERIODS:
            return
        seat = await reports.seat_breakdown(
            session,
            viewer,
            project=project,
            date_from=out.first,
            date_to=out.last,
            period=out.bucket,
            currency=out.currency,
            rate_policy=policy,
            filters=filters,
        )
        if not seat.seat_cost_shown:
            return
        cells = {b.start: b for b in out.buckets}
        plans: dict[int, _Fees] = {}
        for row in seat.rows:
            fees = _Fees(user_id=row.key.get("user"))
            for summary in (row.seat_allocated, row.seat_unallocated):
                if summary is not None:
                    fees.fee += summary.total.amount
                    fees.broken = fees.broken or bool(summary.unconverted)
            cell = cells.get(row.period) if row.period is not None else None
            if cell is not None:
                cell.seat += fees.fee
                cell.unconverted += sum(
                    len(s.unconverted) for s in (row.seat_allocated, row.seat_unallocated) if s is not None
                )
            sub_id = row.key.get("subscription")
            if sub_id is None:
                continue
            fees.api = row.seat_api.total.amount if row.seat_api is not None else Decimal(0)
            plans.setdefault(sub_id, _Fees(user_id=fees.user_id)).add(fees)
        if not plans:
            return

        people: dict[int | None, _Fees] = {}
        total = _Fees(user_id=None)
        for plan in plans.values():
            people.setdefault(plan.user_id, _Fees(user_id=plan.user_id)).add(plan)
            total.add(plan)
        out.leverage_total = total.person()

        def rank(fees: _Fees, tie: int) -> tuple[Decimal, Decimal, int]:
            shown = fees.person()
            return (-(shown.fee or Decimal(0)), -shown.api, tie)

        ranked_people = sorted(people.values(), key=lambda f: rank(f, f.user_id or 0))
        kept_people, rest_people = _top_and_rest(ranked_people)
        out.people = [p.person() for p in kept_people]
        if rest_people:
            out.people_others = _summed(rest_people).person()
            out.people_others_count = len(rest_people)
        if project is None:
            ranked_plans = sorted(plans.items(), key=lambda item: rank(item[1], item[0]))
            kept_plans, rest_plans = _top_and_rest(ranked_plans)
            # Plan names only for the plans listed.
            stmt = select(AiSubscription.id, AiSubscription.plan_name).where(
                AiSubscription.id.in_([sub_id for sub_id, _plan in kept_plans])
            )
            names = {sid: name for sid, name in (await session.execute(stmt)).all()}
            listed = []
            for sub_id, plan in kept_plans:
                person = plan.person()
                listed.append(
                    PlanLeverage(
                        subscription_id=sub_id,
                        plan_name=names.get(sub_id, ""),
                        fee=person.fee,
                        api=person.api,
                        leverage=person.leverage,
                        user_id=plan.user_id,
                    )
                )
            out.leverage = listed
            if rest_plans:
                summed = _summed([plan for _sub_id, plan in rest_plans]).person()
                out.leverage_others = PlanLeverage(
                    subscription_id=0, plan_name="", fee=summed.fee, api=summed.api, leverage=summed.leverage
                )
                out.leverage_others_count = len(rest_plans)

    # -- leverage --------------------------------------------------------------------

    @staticmethod
    async def _leverage(
        session: AsyncSession,
        viewer: User,
        raw: list[dict[str, Any]],
        converted: dict[tuple[int, str], Decimal],
        first: date,
        last: date,
        currency: str,
    ) -> list[PlanLeverage]:
        """Per subscription of *viewer* active in the period: its prorated fee, the value it covered, the ratio.

        The period is the page's dates (the data's where open), narrowed to
        the days the reader's plans run and ending today at the latest, since
        later days are not paid for yet; a period longer than
        :data:`MAX_BUCKETS` months gets no leverage. The fee of each month is
        converted at the month's last day (today for the running month), as
        the spend reports do.
        """
        from specivo.services.exchange_rate_service import ExchangeRateService

        today = utcnow().date()
        last = min(last, today)
        if last < first:
            return []
        stmt = (
            select(AiSubscription)
            .where(
                AiSubscription.user_id == viewer.id,
                AiSubscription.starts_on <= last,
                (AiSubscription.ends_on.is_(None)) | (AiSubscription.ends_on >= first),
            )
            .order_by(AiSubscription.starts_on, AiSubscription.id)
        )
        subscriptions = list((await session.execute(stmt)).scalars().all())
        if not subscriptions:
            return []
        first = max(first, min(sub.starts_on for sub in subscriptions))
        if all(sub.ends_on is not None for sub in subscriptions):
            last = min(last, max(sub.ends_on for sub in subscriptions if sub.ends_on is not None))
        if last < first or bucket_count(first, last, "month") > MAX_BUCKETS:
            return []
        covered: dict[int, Decimal] = defaultdict(Decimal)
        for i, r in enumerate(raw):
            if r["cost_type"] == "subscription" and r["subscription_id"] is not None:
                covered[r["subscription_id"]] += converted.get((i, "api"), Decimal(0))

        fee_items: list[tuple[Money, date]] = []
        owners: list[int] = []
        for sub in subscriptions:
            for year, month in months_between(first, last):
                fee, days = prorated_fee(
                    sub.amount, sub.billing_period, sub.starts_on, sub.ends_on, year, month, first=first, last=last
                )
                if days and fee:
                    fee_items.append((Money(fee, sub.currency_code), min(month_last(year, month), today)))
                    owners.append(sub.id)
        fees: dict[int, Decimal] = defaultdict(Decimal)
        broken: set[int] = set()
        if fee_items:
            results = await ExchangeRateService().convert_many(session, fee_items, currency)
            for owner, result in zip(owners, results, strict=True):
                if isinstance(result, Converted):
                    fees[owner] += result.money.amount
                else:
                    broken.add(owner)
        out: list[PlanLeverage] = []
        for sub in subscriptions:
            plan_fee: Decimal | None = None if sub.id in broken or not fees.get(sub.id) else fees[sub.id]
            api = covered.get(sub.id, Decimal(0))
            if plan_fee is None and not api and sub.id not in broken:
                continue
            ratio = (api / plan_fee).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if plan_fee else None
            out.append(
                PlanLeverage(subscription_id=sub.id, plan_name=sub.plan_name, fee=plan_fee, api=api, leverage=ratio)
            )
        return out
