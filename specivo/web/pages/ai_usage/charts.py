"""The AI usage charts: view models and the per-reader cache."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import date
from decimal import Decimal
from typing import Any

from babel.numbers import format_decimal
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.core.i18n import format_date, get_locale, ngettext
from specivo.core.i18n import gettext as _
from specivo.core.money import Money
from specivo.core.redis import get_redis
from specivo.models.ai_usage_entry import AiUsageEntry
from specivo.models.project import Project
from specivo.models.user import User
from specivo.services.ai_usage_chart_service import (
    NO_PROJECT,
    OTHER,
    TOP_N,
    AiUsageChartService,
    Share,
    UsageCharts,
    color_slots,
)
from specivo.services.permission_service import ai_usage_visibility
from specivo.web.pages.ai_usage._common import NONE_VALUE, _parse_date, _parse_int
from specivo.web.pages.ai_usage.filters import Filters
from specivo.web.pages.ai_usage.rows import _tool_names

#: Token classes of the two token charts: the fresh tokens, and the cache.
_FRESH_TOKENS = ("input", "output", "reasoning")
_CACHE_TOKENS = ("cache_read", "cache_write_5m", "cache_write_1h")


def _token_label(token_class: str) -> str:
    return {
        "input": _("Input"),
        "output": _("Output"),
        "reasoning": _("Reasoning"),
        "cache_read": _("Cache read"),
        "cache_write_5m": _("Cache write 5m"),
        "cache_write_1h": _("Cache write 1h"),
    }[token_class]


def _period_views(charts: UsageCharts) -> list[dict[str, str]]:
    """Axis label, tooltip heading and warning note of every bucket, in the reader's language."""
    out = []
    for b in charts.buckets:
        if charts.bucket == "year":
            label = title = format_date(b.start, "y")
        elif charts.bucket == "quarter":
            label, title = format_date(b.start, "yQQQ"), format_date(b.start, "yQQQQ")
        elif charts.bucket == "month":
            label, title = format_date(b.start, "yMMM"), format_date(b.start, "yMMMM")
        elif charts.bucket == "week":
            label = format_date(b.start, "MMMd")
            title = _("Week of %(date)s") % {"date": format_date(b.start, "yMMMd")}
        else:
            label, title = format_date(b.start, "MMMd"), format_date(b.start, "yMMMEd")
        notes = []
        if b.unpriced:
            notes.append(
                ngettext("%(num)d entry without a price", "%(num)d entries without a price", b.unpriced)
                % {"num": b.unpriced}
            )
        if b.unconverted:
            notes.append(
                ngettext(
                    "%(num)d amount without an exchange rate", "%(num)d amounts without an exchange rate", b.unconverted
                )
                % {"num": b.unconverted}
            )
        out.append({"label": label, "title": title, "note": " · ".join(notes)})
    return out


def _float(value: Decimal) -> float:
    """An amount as a float rounded to cents, for the charts only (the totals show exact amounts)."""
    return round(float(value), 2)


def _group_label(labels: dict[str, str], key: str) -> str:
    if key == OTHER:
        return _("Other")
    return labels.get(key, key) or "–"


def _share_points(charts: UsageCharts, shares: list[Share], projects: bool) -> list[dict[str, Any]]:
    """The points of a breakdown chart. Spend is the pay-per-use spend (seat cost is not split by group)."""
    points = []
    for s in shares:
        if s.key == OTHER:
            points.append({"label": _("Other"), "spend": _float(s.spend), "api": _float(s.api), "other": s.groups})
            continue
        if projects:  # noqa: SIM108
            label = _("Unassigned") if s.key == NO_PROJECT else (s.label or "–")
        else:
            label = s.label or "–"
        points.append({"label": label, "spend": _float(s.spend), "api": _float(s.api)})
    return points


#: The breakdown charts of a chart payload, by the name of their per-chart choice.
BREAKDOWNS = {"user": "by_user", "project": "by_project", "model": "by_model", "tool": "by_tool"}


def fold_share_points(points: list[dict[str, Any]], show: int) -> list[dict[str, Any]]:
    """The first *show* groups of a breakdown by name, the rest (and its "other" point) summed into one.

    All of them when only one would be left over: an "other" of one group is no help.
    """
    named = [p for p in points if not p.get("other")]
    others = [p for p in points if p.get("other")]
    rest = named[show:] + others
    groups = len(named[show:]) + sum(int(p["other"]) for p in others)
    if len(named) <= show or groups <= 1:
        return points
    folded = {
        "label": _("Other"),
        "spend": round(sum(p["spend"] for p in rest), 6),
        "api": round(sum(p["api"] for p in rest), 6),
        "other": groups,
    }
    return [*named[:show], folded]


def fold_breakdowns(view: dict[str, Any] | None, shows: dict[str, int] | None = None) -> dict[str, Any] | None:
    """Fold each breakdown chart of *view* to its chosen number of groups (``TOP_N`` unless *shows* says)."""
    if not view:
        return view
    view = dict(view)
    for name, key in BREAKDOWNS.items():
        chart = view.get(key)
        if chart and chart.get("points"):
            view[key] = {**chart, "points": fold_share_points(chart["points"], (shows or {}).get(name, TOP_N))}
    return view


def _project_label(key: str, labels: dict[str, str]) -> str:
    return _("Unassigned") if key == NO_PROJECT else _group_label(labels, key)


def _leverage_text(value: Decimal | None, locale: str) -> str:
    return f"{format_decimal(value, format='#,##0.00', locale=locale)}×" if value is not None else ""


def _charts_view(charts: UsageCharts) -> dict[str, Any] | None:
    """The data of each chart of the page (``None`` when there is nothing to draw)."""
    if charts.too_long:
        return {"too_long": True}
    if charts.empty:
        return None
    locale = get_locale()
    periods = _period_views(charts)
    base = {"currency": charts.currency, "locale": locale}

    def stack(keys: list[str], attr: str, labels: dict[str, str]) -> dict[str, Any]:
        slots = color_slots(keys)
        return {
            "names": [_group_label(labels, k) for k in keys],
            "values": [[_float(getattr(b, attr).get(k, Decimal(0))) for b in charts.buckets] for k in keys],
            # Group keys and their palette slots: the report tables and person cards take their colours from here.
            "keys": keys,
            "colors": [slots[k] for k in keys],
        }

    has_spend = charts.has_spend
    report = charts.scope != "own"
    stacks: dict[str, Any] = {}
    if report:
        stacks["user"] = stack(charts.users, "api_by_user", charts.user_labels)
    stacks["tool"] = stack(charts.tools, "api_by_tool", charts.tool_labels)
    stacks["model"] = stack(charts.models, "api_by_model", {})
    if charts.scope == "instance":
        project_slots = color_slots(charts.projects)
        stacks["project"] = {
            "names": [_project_label(k, charts.project_labels) for k in charts.projects],
            "values": [[_float(b.api_by_project.get(k, Decimal(0))) for b in charts.buckets] for k in charts.projects],
            "keys": charts.projects,
            "colors": [project_slots[k] for k in charts.projects],
        }
    time = {
        **base,
        "kind": "usage-time",
        "periods": periods,
        "stacks": stacks,
        # On a spend report with seat cost, spend is pay-per-use plus seat cost, and the seat cost has a line too.
        "spend": [_float(b.spend + b.seat) for b in charts.buckets] if has_spend else None,
        "spend_label": _("Spend"),
        "total_label": _("API-equivalent value"),
    }
    if charts.has_seat:
        time["seat"] = [_float(b.seat) for b in charts.buckets]
        time["seat_label"] = _("Seat cost")

    def tokens(classes: tuple[str, ...]) -> dict[str, Any] | None:
        series = [
            {"name": _token_label(c), "values": [b.tokens[c] for b in charts.buckets]}
            for c in classes
            if any(b.tokens[c] for b in charts.buckets)
        ]
        if not series:
            return None
        return {**base, "kind": "usage-tokens", "periods": periods, "series": series, "total_label": _("Total")}

    def share(points: list[dict[str, Any]]) -> dict[str, Any]:
        return {**base, "kind": "usage-share", "points": points, "no_spend": not has_spend}

    def money(value: Decimal | None) -> str:
        return Money(value, charts.currency).format(locale) if value is not None else ""

    leverage = [
        {
            "plan": p.plan_name,
            "fee": money(p.fee),
            "api": money(p.api),
            "leverage": _leverage_text(p.leverage, locale),
            **({"holder": p.holder or "–"} if report else {}),
        }
        for p in charts.leverage
    ]
    view: dict[str, Any] = {
        "time": time,
        "tokens_fresh": tokens(_FRESH_TOKENS),
        "tokens_cache": tokens(_CACHE_TOKENS),
        "by_project": share(_share_points(charts, charts.by_project, projects=True)),
        "by_model": share(_share_points(charts, charts.by_model, projects=False)),
        "bucket": charts.bucket,
        "first": charts.first.isoformat() if charts.first else "",
        "last": charts.last.isoformat() if charts.last else "",
        "has_spend": has_spend,
        "unpriced": charts.unpriced,
        "entries": charts.entries,
        "unconverted": charts.unconverted,
        "leverage": leverage,
    }
    if report:
        view.update(
            scope=charts.scope,
            by_user=share(_share_points(charts, charts.by_user, projects=False)),
            by_tool=share(_share_points(charts, charts.by_tool, projects=False)),
            seat_shown=charts.seat_shown,
            people=[
                {
                    "name": p.label or "–",
                    "fee": money(p.fee),
                    "api": money(p.api),
                    "leverage": _leverage_text(p.leverage, locale),
                }
                for p in charts.people
            ],
            people_others=(
                {
                    "count": charts.people_others_count,
                    "fee": money(charts.people_others.fee),
                    "api": money(charts.people_others.api),
                    "leverage": _leverage_text(charts.people_others.leverage, locale),
                }
                if charts.people_others is not None
                else None
            ),
            leverage_others=(
                {
                    "count": charts.leverage_others_count,
                    "fee": money(charts.leverage_others.fee),
                    "api": money(charts.leverage_others.api),
                    "leverage": _leverage_text(charts.leverage_others.leverage, locale),
                }
                if charts.leverage_others is not None
                else None
            ),
            leverage_total=(
                {
                    "fee": money(charts.leverage_total.fee),
                    "api": money(charts.leverage_total.api),
                    "leverage": _leverage_text(charts.leverage_total.leverage, locale),
                }
                if charts.leverage_total is not None
                else None
            ),
        )
        if charts.scope == "project":
            view.pop("by_project")
    return view


async def cached_chart_payload(
    db: AsyncSession,
    prefix: str,
    parts: dict[str, Any],
    version_stmt: Any,
    build: Callable[[], Awaitable[dict[str, Any] | None]],
) -> dict[str, Any] | None:
    """A chart payload from Redis under ``{prefix}:{digest of parts and data version}``, or *build*'s, then stored.

    *version_stmt* selects what changes when the charted data does (an entry
    count and the latest change); it runs only when caching is on. What it
    cannot see (exchange rates, prices, plans) is at most
    ``ai_usage_chart_cache_seconds`` old. Without Redis the payload is built
    each time.
    """
    ttl = get_settings().ai_usage_chart_cache_seconds
    key = ""
    if ttl:
        version = (await db.execute(version_stmt)).one()
        payload_parts = {"locale": get_locale(), "version": [str(v) for v in version], **parts}
        digest = hashlib.sha256(json.dumps(payload_parts, sort_keys=True, default=str).encode()).hexdigest()[:32]
        key = f"{prefix}:{digest}"
        try:
            redis = await get_redis()
            cached = await redis.get(key)
            if cached:
                payload: dict[str, Any] | None = json.loads(cached)
                return payload
        except Exception:  # noqa: BLE001
            key = ""
    view = await build()
    if key:
        try:  # noqa: SIM105
            await redis.set(key, json.dumps(view), ex=ttl)
        except Exception:  # noqa: BLE001, S110
            pass
    return view


def _cache_value(value: Any) -> Any:
    """A filter value as it goes into a chart cache key."""
    if isinstance(value, Project):
        return value.id
    if isinstance(value, date):
        return value.isoformat()
    return value


async def _cached_charts_view(
    db: AsyncSession, user: User, currency: str, args: dict[str, Any]
) -> dict[str, Any] | None:
    """The chart payload of My AI usage, from Redis when the same view was built in the last minute.

    The key holds the number of the user's entries and their latest change,
    so logging, editing, excluding or deleting an entry shows at once.
    """

    async def build() -> dict[str, Any] | None:
        charts = await AiUsageChartService().charts(
            db, user, currency=currency, tool_names=await _tool_names(db), **args
        )
        return _charts_view(charts)

    version = select(func.count(), func.max(AiUsageEntry.updated_at)).where(AiUsageEntry.user_id == user.id)
    parts = {"currency": currency, **{k: _cache_value(v) for k, v in args.items()}}
    return fold_breakdowns(await cached_chart_payload(db, f"aiu:charts:{user.id}", parts, version, build))


async def _project_charts_view(
    db: AsyncSession,
    user: User,
    project: Project,
    currency: str,
    filters: Filters,
    issue_id: int | None,
    chosen: str,
) -> dict[str, Any] | None:
    """The charts of the project's AI usage tab: the entries the reader may read there, with the tab's filters.

    Counted entries, or the excluded ones when the tab shows only those (as
    on My AI usage). Seat cost and leverage follow the spend report's rules
    (AiUsageChartService.report_charts). Cached in Redis like the report
    charts: keyed by the project, the reader, what the reader may see, the
    filters and the data version (the project's entries and the plans).
    """
    from specivo.models.ai_subscription import AiSubscription
    from specivo.services.ai_spend_report_service import ReportFilters, session_context_filters

    excluded = filters.status == "excluded"
    extra = filters.extra()
    planning = filters.planning
    report_filters = ReportFilters(
        user_id=_parse_int(filters.user_id),
        tool=filters.tool or None,
        model=filters.model or None,
        provider=extra["provider"],
        cost_type=filters.cost_type or None,
        version_id=planning.version_id,
        without_version=planning.without_version,
        sprint_id=planning.sprint_id,
        without_sprint=planning.without_sprint,
        **session_context_filters(filters.attended, filters.profile),
        **filters.billing_fields,
    )
    args: dict[str, Any] = {
        "date_from": _parse_date(filters.from_date),
        "date_to": _parse_date(filters.to_date),
        "issue_id": issue_id,
        "without_issue": filters.issue == NONE_VALUE,
        "branch": extra["branch"],
        "without_branch": extra["without_branch"],
        "excluded": excluded,
    }

    async def build() -> dict[str, Any] | None:
        charts = await AiUsageChartService().report_charts(
            db,
            user,
            project=project,
            currency=currency,
            filters=report_filters,
            bucket=chosen or None,
            tool_names=await _tool_names(db),
            **args,
        )
        return _charts_view(charts)

    version = select(
        func.count(AiUsageEntry.id),
        func.max(AiUsageEntry.updated_at),
        select(func.count(AiSubscription.id)).scalar_subquery(),
        select(func.max(AiSubscription.updated_at)).scalar_subquery(),
    ).where(AiUsageEntry.project_id == project.id)
    parts = {
        "viewer": user.id,
        "admin": bool(user.is_admin),
        "visibility": await ai_usage_visibility(db, user, project),
        "currency": currency,
        "filters": [
            report_filters.user_id,
            report_filters.tool,
            report_filters.model,
            report_filters.provider,
            report_filters.cost_type,
            filters.version,
            filters.sprint,
        ],
        "bucket": chosen,
        **{k: _cache_value(v) for k, v in args.items()},
    }
    view = await cached_chart_payload(db, f"aiu:project-charts:{project.id}:{user.id}", parts, version, build)
    return fold_breakdowns(view)
