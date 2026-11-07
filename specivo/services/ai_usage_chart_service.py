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

Buckets are UTC days for a range of up to :data:`DAY_BUCKETS_MAX` days, ISO
weeks (from Monday) up to :data:`WEEK_BUCKETS_MAX` days, calendar months
beyond. The range is the page's dates, or the days with entries where a date
is left open. Every bucket of the range is listed, empty ones too, so the time
axis is even.

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

from sqlalchemy import func, select
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
from specivo.services.ai_spend_report_service import period_start
from specivo.services.ai_usage_service import AiUsageService

#: Longest range drawn in days; weeks up to WEEK_BUCKETS_MAX, months beyond.
DAY_BUCKETS_MAX = 60
WEEK_BUCKETS_MAX = 731
#: Groups shown by name in a breakdown or a stack; the rest are summed as one "other" group.
TOP_N = 8
#: Key of the "other" group (the page translates it).
OTHER = "\x00other"
#: Key of entries without a project (the page translates it).
NO_PROJECT = "\x00none"


def bucket_size(first: date, last: date) -> str:
    """``"day"``, ``"week"`` or ``"month"``: the bucket for a range from *first* to *last* (inclusive)."""
    days = (last - first).days + 1
    if days <= DAY_BUCKETS_MAX:
        return "day"
    if days <= WEEK_BUCKETS_MAX:
        return "week"
    return "month"


def bucket_starts(first: date, last: date, size: str) -> list[date]:
    """The first day of every *size* bucket from *first*'s bucket to *last*'s, in order."""
    start = period_start(first, size)
    end = period_start(last, size)
    assert start is not None and end is not None
    out: list[date] = []
    while start <= end:
        out.append(start)
        if size == "day":
            start += timedelta(days=1)
        elif size == "week":
            start += timedelta(days=7)
        else:
            start = month_last(start.year, start.month) + timedelta(days=1)
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
        tool_names: dict[str, str] | None = None,
    ) -> UsageCharts:
        """The charts of *viewer*'s own entries matching the filters, amounts in *currency*.

        *excluded* ``True`` charts the excluded entries instead of the counted
        ones. *tool_names* maps tool slugs to names (looked up when left out).
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
        raw = await self._grouped(session, conditions)
        out = UsageCharts(currency=currency)
        if not raw:
            return out

        days = [r["day"] for r in raw]
        first = date_from or min(days)
        last = date_to or max(days)
        if last < first:
            return out
        out.first, out.last = first, last
        out.bucket = bucket_size(first, last)
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
            start = period_start(r["day"], out.bucket)
            bucket = buckets.get(start) if start is not None else None
            if bucket is None:
                continue
            api = converted.get((i, "api"), Decimal(0))
            spend = converted.get((i, "spend"), Decimal(0))
            tool_key, model_key = r["tool"], r["model"] or ""
            project_key = str(r["project_id"]) if r["project_id"] is not None else NO_PROJECT
            bucket.entries += r["entries"]
            bucket.unpriced += r["unpriced"]
            bucket.unconverted += sum((i, m) in unconverted_rows for m in ("api", "spend"))
            bucket.api += api
            bucket.spend += spend
            bucket.api_by_tool[tool_key] += api
            bucket.api_by_model[model_key] += api
            for c, n in r["tokens"].items():
                bucket.tokens[c] += n
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
            out.leverage = await self._leverage(session, viewer, raw, converted, first, last, currency)
        return out

    # -- the grouped query ---------------------------------------------------------

    @staticmethod
    async def _grouped(session: AsyncSession, conditions: list[Any]) -> list[dict[str, Any]]:
        """One row per day x tool x model x project x subscription x currencies, with the sums."""
        keys = {
            "day": AiUsageEntry.usage_date,
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
        stmt = select(*columns, *aggregates).where(*conditions).group_by(*keys.values())
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

        The fee of each month is converted at the month's last day (today for
        the running month), as the spend reports do; the period ends today at
        the latest, since later days are not paid for yet.
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
        covered: dict[int, Decimal] = defaultdict(Decimal)
        for i, r in enumerate(raw):
            if r["cost_type"] == "subscription" and r["subscription_id"] is not None and first <= r["day"] <= last:
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
