"""Chart data for the "My AI usage" page: value and tokens over time, and where the value went.

:meth:`AiUsageChartService.charts` takes the filters of the page and returns
plain numbers for three charts and a short list:

- **over time**: the API-equivalent value per time bucket, split by tool and by
  model (the page switches between the two), the spend per bucket, and the
  number of unpriced entries per bucket, so a bucket that looks cheap because
  its model has no price can say so;
- **tokens over time**: the token classes per bucket (both token buckets,
  base and long-context, summed per class);
- **breakdown**: the API-equivalent value and the spend by project and by
  model, the largest :data:`TOP_N` plus one "other" group;
- **leverage**: per subscription plan of the reader, the API-equivalent value
  of the usage it covered over the plan's fee for the period, when the filters
  leave the whole plan in view.

The entries are those the page lists (``AiUsageService._list_conditions``, so
visibility and every filter apply the same way): counted ones by default, or
the excluded ones when the page shows only those. Amounts are converted into
the reporting currency at each entry's day (the default rate policy, as the
page totals); amounts without a usable rate are left out of the numbers and
counted in ``unconverted`` (the totals box above lists them).

Buckets are UTC days for a range of up to :data:`DAY_MAX_DAYS` days, ISO
weeks (from Monday) up to :data:`WEEK_MAX_DAYS` days, then calendar months,
quarters or years, the first that keeps to :data:`AUTO_MAX_BUCKETS` points;
the reader may pick days, weeks or months instead (up to :data:`MAX_BUCKETS`).
The range runs from the first to the last day with a matching entry (the
page's dates only filter the entries), and every bucket in it is listed,
empty ones too, so the time axis is even. Entries spanning more than
:data:`MAX_BUCKETS` years are not charted (``too_long``).

Query count: one grouped query for the entries, the label lookups (tools,
projects), one or two conversions (two queries each), and the subscriptions.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import TIMESTAMP, Date, and_, case, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.ai_pricing import TOKEN_CLASSES
from specivo.core.ai_subscriptions import month_last, months_between, prorated_fee
from specivo.core.exchange import Converted
from specivo.core.money import Money
from specivo.core.utils import utcnow
from specivo.models.ai_subscription import AiSubscription
from specivo.models.ai_usage_entry import AiUsageEntry
from specivo.models.project import Project
from specivo.models.user import User
from specivo.services.ai_usage_service import AiUsageService

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
#: Key of the "other" group (the page translates it).
OTHER = "\x00other"
#: Key of entries without a project (the page translates it).
NO_PROJECT = "\x00none"


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
    api_by_tool: dict[str, Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    api_by_model: dict[str, Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    tokens: dict[str, int] = field(default_factory=lambda: dict.fromkeys(TOKEN_CLASSES, 0))


@dataclass
class Share:
    """One group of a breakdown: its key, label and amounts."""

    key: str
    label: str
    api: Decimal = Decimal(0)
    spend: Decimal = Decimal(0)
    entries: int = 0


@dataclass
class PlanLeverage:
    """One subscription of the reader over the period: the fee, the value of the usage it covered, the ratio."""

    subscription_id: int
    plan_name: str
    fee: Decimal | None
    api: Decimal
    #: ``api / fee`` (two decimals); None without a fee in the period or a fee that does not convert.
    leverage: Decimal | None


@dataclass
class UsageCharts:
    """What the charts draw; amounts in ``currency``."""

    currency: str
    bucket: str = "day"
    first: date | None = None
    last: date | None = None
    buckets: list[Bucket] = field(default_factory=list)
    #: Stack keys of the time chart, largest first (``OTHER`` last when present).
    tools: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    #: Tool names by slug (models are their own labels).
    tool_labels: dict[str, str] = field(default_factory=dict)
    by_project: list[Share] = field(default_factory=list)
    by_model: list[Share] = field(default_factory=list)
    leverage: list[PlanLeverage] = field(default_factory=list)
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
        return any(b.spend for b in self.buckets)


class AiUsageChartService:
    """Build the charts of the "My AI usage" page (see the module docstring)."""

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
        """The charts of *viewer*'s own entries matching the filters, amounts in *currency*.

        *excluded* ``True`` charts the excluded entries instead of the counted
        ones. *bucket* picks the time bucket (one of :data:`CHOSEN_SIZES`;
        automatic when left out or when it would draw too many). *tool_names*
        maps tool slugs to names (looked up when left out).
        """
        from specivo.services.ai_subscription_service import AiToolService
        from specivo.services.exchange_rate_service import ExchangeRateService

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
        # The range is the days with entries (the page's dates only filter
        # them), so an open or absurd date range never widens it; one indexed
        # min/max before anything is grouped.
        span = (
            await session.execute(
                select(func.min(AiUsageEntry.usage_date), func.max(AiUsageEntry.usage_date)).where(*conditions)
            )
        ).one()
        if span[0] is None:
            return out
        first, last = span[0], span[1]
        size = bucket_size(first, last, bucket)
        if bucket_count(first, last, size) > MAX_BUCKETS:
            out.too_long = True
            return out
        out.first, out.last = first, last
        out.bucket = size
        raw = await self._grouped(session, conditions, size, currency)
        buckets = {start: Bucket(start=start) for start in bucket_starts(first, last, out.bucket)}

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
            results = await ExchangeRateService().convert_many(session, items, currency)
            for slot, result in zip(slots, results, strict=True):
                if isinstance(result, Converted):
                    converted[slot] = result.money.amount
                else:
                    unconverted_rows.add(slot)

        tool_totals: dict[str, Decimal] = defaultdict(Decimal)
        model_totals: dict[str, Decimal] = defaultdict(Decimal)
        project_totals: dict[str, Share] = {}
        model_shares: dict[str, Share] = {}
        for i, r in enumerate(raw):
            cell = buckets.get(bucket_start(r["day"], out.bucket))
            if cell is None:
                continue
            api = converted.get((i, "api"), Decimal(0))
            spend = converted.get((i, "spend"), Decimal(0))
            tool_key, model_key = r["tool"], r["model"] or ""
            project_key = str(r["project_id"]) if r["project_id"] is not None else NO_PROJECT
            cell.entries += r["entries"]
            cell.unpriced += r["unpriced"]
            cell.unconverted += sum((i, m) in unconverted_rows for m in ("api", "spend"))
            cell.api += api
            cell.spend += spend
            cell.api_by_tool[tool_key] += api
            cell.api_by_model[model_key] += api
            for c, n in r["tokens"].items():
                cell.tokens[c] += n
            tool_totals[tool_key] += api
            model_totals[model_key] += api
            for shares, key in ((project_totals, project_key), (model_shares, model_key)):
                share = shares.setdefault(key, Share(key=key, label=key))
                share.api += api
                share.spend += spend
                share.entries += r["entries"]
            out.entries += r["entries"]
            out.unpriced += r["unpriced"]
        out.unconverted = len(unconverted_rows)

        out.tools = self._stack_keys(tool_totals, buckets.values(), "api_by_tool")
        out.models = self._stack_keys(model_totals, buckets.values(), "api_by_model")
        out.buckets = list(buckets.values())

        names = tool_names
        if names is None:
            names = {t.slug: t.name for t in await AiToolService().list_tools(session, active_only=False)}
        out.tool_labels = {k: names.get(k, k) for k in tool_totals}
        project_ids = [int(k) for k in project_totals if k != NO_PROJECT]
        project_keys: dict[int, str] = {}
        if project_ids:
            stmt = select(Project.id, Project.key).where(Project.id.in_(project_ids))
            project_keys = {pid: key for pid, key in (await session.execute(stmt)).all()}
        for key, share in project_totals.items():
            if key != NO_PROJECT:
                share.label = project_keys.get(int(key), "")
        out.by_project = self._top_shares(project_totals)
        out.by_model = self._top_shares(model_shares)

        if not excluded and not (project or unassigned or tool or model) and cost_type in (None, "subscription"):
            out.leverage = await self._leverage(
                session, viewer, raw, converted, date_from or first, date_to or last, currency
            )
        return out

    # -- the grouped query ---------------------------------------------------------

    @staticmethod
    async def _grouped(session: AsyncSession, conditions: list[Any], size: str, currency: str) -> list[dict[str, Any]]:
        """One row per bucket x tool x model x project x subscription x currencies, with the sums.

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
        """The largest shares by API-equivalent value, the rest summed into one ``OTHER`` share."""
        keep = top_groups({k: s.api for k, s in shares.items()})
        out = [shares[k] for k in keep]
        rest = [s for k, s in shares.items() if k not in set(keep)]
        if rest:
            other = Share(key=OTHER, label=OTHER)
            for s in rest:
                other.api += s.api
                other.spend += s.spend
                other.entries += s.entries
            out.append(other)
        return out

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
