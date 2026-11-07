"""Web pages for AI usage: the project tab, My AI usage and the manual entry form.

- ``/projects/{key}/ai-usage/``: the project's entries the reader may see,
  with filters and totals in the project's reporting currency.
- the issue tab (``/partials/issues/{key}/ai-usage/``) is :mod:`specivo.web.pages.ai_issue_usage`.
- ``/my/ai-usage/``: the entries logged in the reader's name across projects,
  unassigned ones included, with bulk reassignment to a project or issue.
- ``/projects/{key}/ai-usage/new/`` and ``/my/ai-usage/new/``: log usage by hand.
- ``/ai-usage/{id}/edit/``, ``/exclude/``, ``/include/``, ``/delete/``.

Every rule is :class:`AiUsageService`'s (visibility, who may log, edit,
exclude and delete); the pages only hide the controls a reader could not use.
Plain server-rendered forms: a form that fails validation is rendered again
with what was typed and the error, a successful write redirects with a
``notice`` code. Usage dates are UTC days.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any
from urllib.parse import urlencode, urlsplit, urlunsplit

from babel.numbers import format_decimal
from fastapi import APIRouter, Depends, Form, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.ai_pricing import TOKEN_CLASSES
from specivo.core.ai_report_limits import enforce_ai_report_rate_limit
from specivo.core.config import get_settings
from specivo.core.database import DB_SESSION
from specivo.core.exceptions import AppError, NotFoundError, PermissionDeniedError
from specivo.core.exchange import ConversionSummary
from specivo.core.i18n import format_date, get_locale, ngettext
from specivo.core.i18n import gettext as _
from specivo.core.money import Money, MoneyError, currency_display_name, format_decimal_plain
from specivo.core.redis import get_redis
from specivo.core.utils import utcnow
from specivo.models.ai_usage_entry import AI_USAGE_COST_TYPES, AiUsageEntry
from specivo.models.issue import Issue
from specivo.models.project import Project
from specivo.models.role import AI_USAGE_VISIBILITY_ALL
from specivo.models.user import User
from specivo.services.ai_model_price_service import AiModelPriceService
from specivo.services.ai_subscription_service import AiSubscriptionService, AiToolService
from specivo.services.ai_usage_chart_service import (
    CHOSEN_SIZES,
    NO_PROJECT,
    OTHER,
    AiUsageChartService,
    Share,
    UsageCharts,
    color_slots,
)
from specivo.services.ai_usage_service import AiUsageInput, AiUsageService, EntryReferences, UsageSummary
from specivo.services.currency_service import CurrencyService
from specivo.services.exchange_rate_service import ExchangeRateService
from specivo.services.issue_service import IssueService
from specivo.services.permission_service import Permission, ai_usage_visibility, check_permission
from specivo.services.project_service import ProjectService
from specivo.web.deps import get_templates, require_user, safe_next_path

router = APIRouter(tags=["web-ai-usage"], include_in_schema=False)

_service = AiUsageService()
_projects = ProjectService()
_issues = IssueService()
_tools = AiToolService()

MY_URL = "/my/ai-usage/"
#: Filter value for "none": entries without an issue, a branch or a project.
NONE_VALUE = "-"

_NOTICES = frozenset({"logged", "saved", "excluded", "included", "deleted", "reassigned"})
#: Error codes a redirect may carry back to a list (bulk reassignment, row actions).
_LIST_ERRORS = frozenset(
    {
        "nothing_selected",
        "project_unknown",
        "issue_unknown",
        "issue_other_project",
        "permission_denied",
        "ai_usage_target_not_member",
        "reassign_failed",
    }
)
#: Token classes behind the "more tokens" disclosure of the form.
_EXTRA_TOKENS = ("cache_read", "cache_write_5m", "cache_write_1h", "reasoning")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


class _FormError(Exception):
    """A form value the page itself refuses (before the service sees it)."""

    def __init__(self, code: str, field_name: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.field = field_name


def _redirect(url: str, params: dict[str, str] | None = None) -> RedirectResponse:
    """303 to *url* with *params* added to its query (empty values dropped)."""
    extra = {k: v for k, v in (params or {}).items() if v}
    if not extra:
        return RedirectResponse(url, 303)
    parts = urlsplit(url)
    query = "&".join(q for q in (parts.query, urlencode(extra)) if q)
    return RedirectResponse(urlunsplit(("", "", parts.path, query, parts.fragment)), 303)


def _next_or(raw: str | None, fallback: str) -> str:
    return safe_next_path(raw) or fallback


def _parse_date(value: str | None) -> date | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _parse_int(value: str | None) -> int | None:
    text = (value or "").strip()
    return int(text) if text.isdigit() else None


def _fmt_int(value: int, locale: str) -> str:
    return format_decimal(value, locale=locale)


def _error_of(exc: Exception) -> dict[str, str]:
    if isinstance(exc, _FormError):
        return {"code": exc.code, "field": exc.field}
    if isinstance(exc, AppError):
        return {"code": exc.code, "field": exc.field or ""}
    raise exc


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


@dataclass
class Filters:
    """The list filters as typed (strings) and as the service takes them."""

    user_id: str = ""
    tool: str = ""
    model: str = ""
    cost_type: str = ""
    issue: str = ""
    from_date: str = ""
    to_date: str = ""
    status: str = ""
    project: str = ""
    provider: str = ""
    branch: str = ""
    errors: list[str] = field(default_factory=list)

    def query(self) -> str:
        """The filters as an urlencoded query string, for pagination and return links."""
        values = {
            "user_id": self.user_id,
            "tool": self.tool,
            "model": self.model,
            "cost_type": self.cost_type,
            "issue": self.issue,
            "from_date": self.from_date,
            "to_date": self.to_date,
            "status": self.status,
            "project": self.project,
            "provider": self.provider,
            "branch": self.branch,
        }
        return urlencode({k: v for k, v in values.items() if v})

    @property
    def excluded(self) -> bool | None:
        return {"counted": False, "excluded": True}.get(self.status)

    def extra(self) -> dict[str, Any]:
        """The provider and branch filters as ``list_entries`` takes them ("-": entries without a branch)."""
        return {
            "provider": self.provider or None,
            "branch": self.branch if self.branch and self.branch != NONE_VALUE else None,
            "without_branch": self.branch == NONE_VALUE,
        }


def _filters(
    *,
    user_id: str = "",
    tool: str = "",
    model: str = "",
    cost_type: str = "",
    issue: str = "",
    from_date: str = "",
    to_date: str = "",
    status: str = "",
    project: str = "",
    provider: str = "",
    branch: str = "",
) -> Filters:
    return Filters(
        user_id=user_id.strip() if user_id.strip().isdigit() else "",
        tool=tool.strip().lower()[:50],
        model=model.strip()[:200],
        cost_type=cost_type if cost_type in AI_USAGE_COST_TYPES else "",
        issue=issue.strip().upper()[:50],
        from_date=from_date if _parse_date(from_date) else "",
        to_date=to_date if _parse_date(to_date) else "",
        status=status if status in ("counted", "excluded") else "",
        project=project.strip().upper()[:50],
        provider=provider.strip().lower()[:50],
        branch=branch.strip()[:255],
    )


async def _issue_filter(db: AsyncSession, user: User, key: str, project: Project | None) -> int | None:
    """The id of the issue *key* the reader may see (in *project* when given); -1 for none, so nothing matches.

    ``"-"`` (entries without an issue) is handled by the caller (``without_issue``).
    """
    if not key or key == NONE_VALUE:
        return None
    try:
        issue = await _issues.get_by_display_key(db, key, user=user)
    except NotFoundError:
        return -1
    if project is not None and issue.project_id != project.id:
        return -1
    return issue.id


# ---------------------------------------------------------------------------
# Rows and totals
# ---------------------------------------------------------------------------


async def _tool_names(db: AsyncSession) -> dict[str, str]:
    return {t.slug: t.name for t in await _tools.list_tools(db, active_only=False)}


def _user_label(refs: EntryReferences, user_id: int) -> str:
    user = refs.users.get(user_id)
    if user is None:
        return ""
    return user.display_name or user.login


async def _rows(
    db: AsyncSession, viewer: User, entries: list[AiUsageEntry], refs: EntryReferences
) -> list[dict[str, Any]]:
    """The entries as the templates show them, with what *viewer* may do to each."""
    locale = get_locale()
    names = await _tool_names(db)
    rows = []
    for e in entries:
        editable = await _service.can_edit(db, viewer, e)
        deletable = _service.can_delete_locked(viewer, e) if e.is_locked else editable
        # Per-class numbers include the long-context bucket; its share is shown apart as well.
        tokens = e.all_tokens()
        long_total = sum(e.long_context_tokens().values())
        cache = tokens["cache_read"] + tokens["cache_write_5m"] + tokens["cache_write_1h"]
        issue_key = refs.issue_keys.get(e.issue_id) if e.issue_id is not None else None
        marginal, api_eq, reported = e.marginal, e.api_equivalent, e.reported_cost
        rows.append(
            {
                "id": e.id,
                "usage_date": e.usage_date.isoformat(),
                "user_id": e.user_id,
                "user_name": _user_label(refs, e.user_id),
                "on_behalf": e.reporter_user_id != e.user_id,
                "reporter_name": _user_label(refs, e.reporter_user_id),
                "key_name": refs.key_names.get(e.reporter_api_key_id or 0, ""),
                "source": e.source,
                "locked": e.is_locked,
                "tool": names.get(e.tool_slug, e.tool_slug),
                "provider": e.provider or "",
                "model": e.model,
                "total_tokens": _fmt_int(sum(tokens.values()), locale),
                "input": _fmt_int(tokens["input"], locale),
                "output": _fmt_int(tokens["output"], locale),
                "cache": _fmt_int(cache, locale),
                "reasoning": _fmt_int(tokens["reasoning"], locale),
                "long_context": _fmt_int(long_total, locale) if long_total else "",
                "cost_type": e.cost_type,
                "cost_basis": e.cost_basis,
                "marginal": marginal.format(locale) if marginal is not None else "",
                "api_equivalent": api_eq.format(locale) if api_eq is not None else "",
                "reported": reported.format(locale) if reported is not None else "",
                "project_key": refs.project_keys.get(e.project_id, "") if e.project_id is not None else "",
                "issue_key": issue_key or "",
                "branch": e.git_branch or "",
                "comments": e.comments or "",
                "excluded": e.is_excluded,
                "editable": editable,
                "deletable": deletable,
            }
        )
    return rows


def _money_view(summary: ConversionSummary, locale: str) -> dict[str, Any]:
    return {
        "total": summary.total.format(locale),
        "converted": summary.converted_count,
        "unconverted": [
            {"amount": g.amount.format(locale), "reason": str(g.reason), "count": g.count} for g in summary.unconverted
        ],
        "rate_dates": [d.isoformat() for d in summary.rate_dates],
    }


async def _totals(db: AsyncSession, summary: UsageSummary, currency: str) -> dict[str, Any]:
    """Token sums and both costs in *currency*; amounts without a usable rate are listed, never dropped."""
    locale = get_locale()
    rates = ExchangeRateService()
    spend = await rates.summarize(db, summary.marginal, currency)
    api_equivalent = await rates.summarize(db, summary.api_equivalent, currency)
    tokens = summary.tokens
    return {
        "entries": summary.entries,
        "excluded": summary.excluded,
        "unpriced": summary.unpriced,
        "unpriced_models": [{"model": m, "count": n} for m, n in summary.unpriced_models],
        "currency": currency,
        "total_tokens": _fmt_int(summary.total_tokens, locale),
        "input": _fmt_int(tokens.get("input", 0), locale),
        "output": _fmt_int(tokens.get("output", 0), locale),
        "cache": _fmt_int(
            tokens.get("cache_read", 0) + tokens.get("cache_write_5m", 0) + tokens.get("cache_write_1h", 0), locale
        ),
        "spend": _money_view(spend, locale),
        "api_equivalent": _money_view(api_equivalent, locale),
    }


# ---------------------------------------------------------------------------
# Charts (My AI usage)
# ---------------------------------------------------------------------------

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
            label = _("Other")
        elif projects:
            label = _("Unassigned") if s.key == NO_PROJECT else (s.label or "–")
        else:
            label = s.label or "–"
        points.append({"label": label, "spend": _float(s.spend), "api": _float(s.api)})
    return points


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


def _bucket_links(filters: Filters, chosen: str, url: str = MY_URL) -> list[dict[str, Any]]:
    """The chart's bucket switch: automatic, or a size the reader picks (the server still caps the points)."""
    base = filters.query()
    out = []
    for value, label in (("", _("Auto")), ("day", _("Day")), ("week", _("Week")), ("month", _("Month"))):
        query = "&".join(q for q in (base, urlencode({"bucket": value}) if value else "") if q)
        out.append(
            {"value": value, "label": label, "url": url + (f"?{query}" if query else ""), "active": value == chosen}
        )
    return out


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
        except Exception:
            key = ""
    view = await build()
    if key:
        try:
            await redis.set(key, json.dumps(view), ex=ttl)
        except Exception:
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
    return await cached_chart_payload(db, f"aiu:charts:{user.id}", parts, version, build)


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
    from specivo.services.ai_spend_report_service import ReportFilters

    excluded = filters.status == "excluded"
    extra = filters.extra()
    report_filters = ReportFilters(
        user_id=_parse_int(filters.user_id),
        tool=filters.tool or None,
        model=filters.model or None,
        provider=extra["provider"],
        cost_type=filters.cost_type or None,
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
        ],
        "bucket": chosen,
        **{k: _cache_value(v) for k, v in args.items()},
    }
    return await cached_chart_payload(db, f"aiu:project-charts:{project.id}:{user.id}", parts, version, build)


async def _filter_choices(db: AsyncSession) -> dict[str, Any]:
    return {
        "tools": [{"slug": t.slug, "name": t.name} for t in await _tools.list_tools(db, active_only=False)],
        "cost_types": list(AI_USAGE_COST_TYPES),
    }


# ---------------------------------------------------------------------------
# Project tab
# ---------------------------------------------------------------------------


async def _project_for(db: AsyncSession, user: User, project_key: str) -> Project:
    project = await _projects.get_by_key(db, project_key.upper())
    await _projects.require_project_access(db, project, user)
    return project


@router.get("/projects/{project_key}/ai-usage/", response_class=HTMLResponse)
async def project_ai_usage(
    project_key: str,
    request: Request,
    response: Response,
    user: Annotated[User, Depends(require_user)],
    db: AsyncSession = DB_SESSION,
    user_id: str = Query(""),
    tool: str = Query(""),
    model: str = Query(""),
    cost_type: str = Query(""),
    issue: str = Query(""),
    from_date: str = Query(""),
    to_date: str = Query(""),
    status: str = Query(""),
    provider: str = Query(""),
    branch: str = Query(""),
    bucket: str = Query(""),
    offset: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=100),
    notice: str = Query(""),
    error: str = Query(""),
) -> Response:
    """The project's AI usage the reader may see, with filters, totals in its reporting currency, and charts.

    ``provider`` and ``branch`` (``-``: none) and ``issue=-`` (no issue) come
    from the spend report's drill-through. The charts group the project's
    usage, so the page spends the reader's AI report budget
    (specivo.core.ai_report_limits); the bucket asked for is only a wish.
    """
    project = await _project_for(db, user, project_key)
    await enforce_ai_report_rate_limit(request, response, user)
    chosen = bucket if bucket in CHOSEN_SIZES else ""
    filters = _filters(
        user_id=user_id,
        tool=tool,
        model=model,
        cost_type=cost_type,
        issue=issue,
        from_date=from_date,
        to_date=to_date,
        status=status,
        provider=provider,
        branch=branch,
    )
    issue_id = await _issue_filter(db, user, filters.issue, project)
    common: dict[str, Any] = {
        **filters.extra(),
        "without_issue": filters.issue == NONE_VALUE,
        "project": project,
        "user_id": _parse_int(filters.user_id),
        "date_from": _parse_date(filters.from_date),
        "date_to": _parse_date(filters.to_date),
        "tool": filters.tool or None,
        "model": filters.model or None,
        "cost_type": filters.cost_type or None,
        "issue_id": issue_id,
    }
    entries, total = await _service.list_entries(
        db, user, excluded=filters.excluded, offset=offset, limit=limit, **common
    )
    refs = await _service.references(db, user, entries)
    currency = await CurrencyService().reporting_currency_for(db, project)
    summary = await _service.summarize_entries(db, user, **common)

    charts_view = await _project_charts_view(db, user, project, currency, filters, issue_id, chosen)
    base_url = f"/projects/{project.key}/ai-usage/"
    page_query = "&".join(q for q in (filters.query(), urlencode({"bucket": chosen}) if chosen else "") if q)
    sees_all = user.is_admin or await ai_usage_visibility(db, user, project) == AI_USAGE_VISIBILITY_ALL
    members = await _projects.list_members(db, project) if sees_all else []
    can_log = await check_permission(user, project.id, Permission.LOG_AI_USAGE, db)
    return get_templates().TemplateResponse(
        request,
        "pages/ai_usage/project.html",
        context={
            "user": user,
            "active_page": "ai_usage",
            "active_project": project,
            "project": project,
            "rows": await _rows(db, user, entries, refs),
            "totals": await _totals(db, summary, currency),
            "total": total,
            "offset": offset,
            "limit": limit,
            "filters": filters,
            "page_query": page_query,
            "charts": charts_view,
            "chart_bucket": chosen,
            "bucket_links": _bucket_links(filters, chosen, base_url),
            "issue_unknown": issue_id == -1,
            "members": [{"id": m["user_id"], "name": m["display_name"] or m["login"]} for m in members],
            "can_log": can_log,
            "return_to": request.url.path + (f"?{request.url.query}" if request.url.query else ""),
            "notice": notice if notice in _NOTICES else "",
            "error": error if error in _LIST_ERRORS else "",
            **await _filter_choices(db),
        },
    )


# ---------------------------------------------------------------------------
# My AI usage
# ---------------------------------------------------------------------------


@router.get(MY_URL, response_class=HTMLResponse)
async def my_ai_usage(
    request: Request,
    response: Response,
    user: Annotated[User, Depends(require_user)],
    db: AsyncSession = DB_SESSION,
    project: str = Query(""),
    tool: str = Query(""),
    model: str = Query(""),
    cost_type: str = Query(""),
    from_date: str = Query(""),
    to_date: str = Query(""),
    status: str = Query(""),
    bucket: str = Query(""),
    offset: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=100),
    notice: str = Query(""),
    error: str = Query(""),
) -> Response:
    """The entries logged in the reader's name, across projects and unassigned, with bulk reassignment.

    The charts group the reader's whole usage, so the page spends the
    reader's AI report budget (specivo.core.ai_report_limits); the bucket
    asked for is only a wish, the chart service caps the points.
    """
    await enforce_ai_report_rate_limit(request, response, user)
    chosen = bucket if bucket in CHOSEN_SIZES else ""
    filters = _filters(
        tool=tool,
        model=model,
        cost_type=cost_type,
        from_date=from_date,
        to_date=to_date,
        status=status,
        project=project,
    )
    selected_project: Project | None = None
    unassigned: bool | None = None
    if filters.project == "-":
        unassigned = True
    elif filters.project:
        try:
            selected_project = await _project_for(db, user, filters.project)
        except (NotFoundError, PermissionDeniedError):
            filters.project = ""
    common: dict[str, Any] = {
        "project": selected_project,
        "user_id": user.id,
        "date_from": _parse_date(filters.from_date),
        "date_to": _parse_date(filters.to_date),
        "tool": filters.tool or None,
        "model": filters.model or None,
        "cost_type": filters.cost_type or None,
        "unassigned": unassigned,
    }
    entries, total = await _service.list_entries(
        db, user, excluded=filters.excluded, offset=offset, limit=limit, **common
    )
    refs = await _service.references(db, user, entries)
    currency = await CurrencyService().reporting_currency_for(db, selected_project)
    summary = await _service.summarize_entries(db, user, **common)
    loggable = await _service.loggable_projects(db, user)
    chart_args: dict[str, Any] = {k: v for k, v in common.items() if k != "user_id"}
    chart_args.update(excluded=filters.status == "excluded", bucket=chosen or None)
    charts_view = await _cached_charts_view(db, user, currency, chart_args)
    page_query = "&".join(q for q in (filters.query(), urlencode({"bucket": chosen}) if chosen else "") if q)
    return get_templates().TemplateResponse(
        request,
        "pages/ai_usage/my.html",
        context={
            "user": user,
            "active_page": "ai-usage",
            "rows": await _rows(db, user, entries, refs),
            "totals": await _totals(db, summary, currency),
            "charts": charts_view,
            "chart_bucket": chosen,
            "bucket_links": _bucket_links(filters, chosen),
            "total": total,
            "offset": offset,
            "limit": limit,
            "filters": filters,
            "page_query": page_query,
            "projects": [{"key": p.key, "name": p.name} for p in loggable],
            "return_to": request.url.path + (f"?{request.url.query}" if request.url.query else ""),
            "notice": notice if notice in _NOTICES else "",
            "error": error if error in _LIST_ERRORS else "",
            **await _filter_choices(db),
        },
    )


@router.post(MY_URL + "reassign/", response_model=None)
async def my_ai_usage_reassign(
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: AsyncSession = DB_SESSION,
    entry_ids: list[str] = Form([]),  # noqa: B008
    target_project: str = Form(""),
    target_issue: str = Form(""),
    next: str = Form(""),  # noqa: A002
) -> Response:
    """Move the selected entries (one's own) to a project, an issue, or back to unassigned; all or nothing."""
    back = _next_or(next, MY_URL)
    ids = sorted({int(v) for v in entry_ids if v.strip().isdigit()})
    if not ids:
        return _redirect(back, {"error": "nothing_selected"})
    try:
        project, issue = await _target(db, user, target_project, target_issue)
    except _FormError as exc:
        return _redirect(back, {"error": exc.code})
    savepoint = await db.begin_nested()
    try:
        for entry_id in ids:
            entry = await _service.get_visible(db, entry_id, user)
            if entry.user_id != user.id:
                raise NotFoundError(f"AI usage entry {entry_id} not found")
            await _service.update(
                db,
                user,
                entry,
                {"project_id": project.id if project else None, "issue_id": issue.id if issue else None},
                request=request,
            )
    except AppError as exc:
        await savepoint.rollback()
        code = exc.code if exc.code in _LIST_ERRORS else "reassign_failed"
        return _redirect(back, {"error": code})
    await savepoint.commit()
    return _redirect(back, {"notice": "reassigned"})


async def _target(
    db: AsyncSession, user: User, project_key: str, issue_key: str
) -> tuple[Project | None, Issue | None]:
    """The project and issue a form names; the issue alone names its project. Raises ``_FormError``."""
    project: Project | None = None
    issue: Issue | None = None
    key = project_key.strip().upper()
    if key:
        try:
            project = await _project_for(db, user, key)
        except (NotFoundError, PermissionDeniedError):
            raise _FormError("project_unknown", "project") from None
    ref = issue_key.strip().upper()
    if ref:
        try:
            issue = await _issues.get_by_display_key(db, ref, user=user)
        except NotFoundError:
            raise _FormError("issue_unknown", "issue") from None
        if project is None:
            project = await _project_for(db, user, issue.project_key)
        elif issue.project_id != project.id:
            raise _FormError("issue_other_project", "issue")
    return project, issue


# ---------------------------------------------------------------------------
# Entry form (log and edit)
# ---------------------------------------------------------------------------


def _blank_form(project: Project | None, currency: str) -> dict[str, Any]:
    return {
        "project": project.key if project else "",
        "issue": "",
        "on_behalf_of": "",
        "tool": "claude-code",
        "provider": "",
        "model": "",
        "usage_date": utcnow().date().isoformat(),
        "tokens": {c: "" for c in TOKEN_CLASSES},
        "request_count": "",
        "cost_type": "",
        "cost_amount": "",
        "cost_currency": currency,
        "comments": "",
    }


def _form_of(entry: AiUsageEntry, refs: EntryReferences, currency: str) -> dict[str, Any]:
    reported = entry.reported_cost
    return {
        "project": refs.project_keys.get(entry.project_id, "") if entry.project_id is not None else "",
        "issue": refs.issue_keys.get(entry.issue_id, "") if entry.issue_id is not None else "",
        "on_behalf_of": "",
        "tool": entry.tool_slug,
        "provider": entry.provider or "",
        "model": entry.model,
        "usage_date": entry.usage_date.isoformat(),
        "tokens": {c: str(v) for c, v in entry.tokens().items()},
        "request_count": str(entry.request_count or ""),
        "cost_type": entry.cost_type,
        "cost_amount": format_decimal_plain(reported.amount) if reported is not None else "",
        "cost_currency": reported.currency if reported is not None else currency,
        "comments": entry.comments or "",
    }


def _read_form(
    *,
    project: str,
    issue: str,
    on_behalf_of: str,
    tool: str,
    provider: str,
    model: str,
    usage_date: str,
    tokens: dict[str, str],
    request_count: str,
    cost_type: str,
    cost_amount: str,
    cost_currency: str,
    comments: str,
) -> dict[str, Any]:
    return {
        "project": project.strip().upper(),
        "issue": issue.strip().upper(),
        "on_behalf_of": on_behalf_of.strip(),
        "tool": tool.strip().lower(),
        "provider": provider.strip(),
        "model": model.strip(),
        "usage_date": usage_date.strip(),
        "tokens": {c: (tokens.get(c) or "").strip() for c in TOKEN_CLASSES},
        "request_count": request_count.strip(),
        "cost_type": cost_type if cost_type in AI_USAGE_COST_TYPES else "",
        "cost_amount": cost_amount.strip(),
        "cost_currency": cost_currency.strip().upper(),
        "comments": comments,
    }


def _usage_values(form: dict[str, Any]) -> dict[str, Any]:
    """The usage fields of a submitted form, parsed; raises ``_FormError`` on what is not a number or a date."""
    day = _parse_date(form["usage_date"])
    if day is None:
        raise _FormError("date_invalid", "usage_date")
    counts: dict[str, int] = {}
    for name, raw in form["tokens"].items():
        if not raw:
            counts[name] = 0
        elif raw.isdigit():
            counts[name] = int(raw)
        else:
            raise _FormError("tokens_invalid", name)
    if not any(counts.values()):
        raise _FormError("tokens_missing", "input")
    raw_count = form["request_count"]
    if raw_count and not raw_count.isdigit():
        raise _FormError("request_count_invalid", "request_count")
    cost: Money | None = None
    if form["cost_amount"]:
        try:
            amount = Decimal(form["cost_amount"].replace(",", "."))
            cost = Money(amount, form["cost_currency"])
        except (InvalidOperation, MoneyError, ValueError):
            raise _FormError("amount_invalid", "cost_amount") from None
    return {
        "tool": form["tool"],
        "provider": form["provider"] or None,
        "model": form["model"],
        "usage_date": day,
        "tokens": counts,
        "request_count": int(raw_count) if raw_count else 0,
        "cost_type": form["cost_type"] or None,
        "reported_cost": cost,
    }


async def _form_choices(
    db: AsyncSession, user: User, form: dict[str, Any], *, fixed_project: Project | None, entry: AiUsageEntry | None
) -> dict[str, Any]:
    """Pickers: tools, model suggestions, currencies, projects; and the subscriptions behind "Automatic"."""
    locale = get_locale()
    tools = [t for t in await _tools.list_tools(db, active_only=False) if t.is_active or t.slug == form["tool"]]
    currencies = await CurrencyService().list_currencies(db, active_only=True, include=form["cost_currency"] or None)
    projects = [] if fixed_project else await _service.loggable_projects(db, user)
    if entry is not None and entry.project_id is not None and all(p.id != entry.project_id for p in projects):
        # The entry's own project stays choosable, even where the reader may not log (a manager's edit).
        current = await db.get(Project, entry.project_id)
        if current is not None:
            projects = [*projects, current]
    owner_id = entry.user_id if entry is not None else user.id
    # Subscriptions are private to their owner and admins (the register's rule): a
    # manager editing someone else's entry sees no plan list.
    subs: list[Any] = []
    if owner_id == user.id or user.is_admin:
        subs, _total = await AiSubscriptionService().list_subscriptions(
            db, user_id=owner_id, active_on=utcnow().date(), limit=20
        )
    if fixed_project is not None:
        on_behalf = await _service.may_log_for_others(db, user, fixed_project)
    else:
        on_behalf = await _service.may_log_for_others_anywhere(db, user)
    return {
        "tools": [{"slug": t.slug, "name": t.name, "provider": t.default_provider or ""} for t in tools],
        "models": [{"provider": p, "model": m} for p, m in await AiModelPriceService().model_suggestions(db)],
        "currencies": [
            {"code": c.code, "name": currency_display_name(c.code, locale, fallback=c.name)} for c in currencies
        ],
        "projects": [{"key": p.key, "name": p.name} for p in projects],
        "subscriptions": [{"name": s.plan_name, "provider": s.provider} for s in subs],
        "can_log_for_others": on_behalf and entry is None,
        "cost_types": list(AI_USAGE_COST_TYPES),
        "extra_tokens": _EXTRA_TOKENS,
    }


async def _render_form(
    request: Request,
    db: AsyncSession,
    user: User,
    *,
    form: dict[str, Any],
    fixed_project: Project | None,
    form_action: str,
    back_url: str,
    next_url: str,
    entry: AiUsageEntry | None = None,
    entry_row: dict[str, Any] | None = None,
    error: dict[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    context: dict[str, Any] = {
        "user": user,
        "active_page": "ai_usage" if fixed_project else "ai-usage",
        "project": fixed_project,
        "form": form,
        "form_action": form_action,
        "back_url": back_url,
        "next_url": next_url,
        "entry": entry_row,
        "locked": bool(entry is not None and entry.is_locked),
        "error": error or {},
        **await _form_choices(db, user, form, fixed_project=fixed_project, entry=entry),
    }
    if fixed_project is not None:
        context["active_project"] = fixed_project
    return get_templates().TemplateResponse(
        request, "pages/ai_usage/form.html", context=context, status_code=status_code
    )


async def _require_can_open_form(db: AsyncSession, user: User, project: Project | None) -> None:
    """403 on the project form for whoever may log neither own usage nor others' there."""
    if project is None:
        return
    if await check_permission(user, project.id, Permission.LOG_AI_USAGE, db):
        return
    if await _service.may_log_for_others(db, user, project):
        return
    raise PermissionDeniedError("Permission 'log_ai_usage' denied for this project")


@router.get("/projects/{project_key}/ai-usage/new/", response_class=HTMLResponse)
async def project_ai_usage_form(
    project_key: str,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: AsyncSession = DB_SESSION,
    issue: str = Query(""),
    next: str = Query(""),  # noqa: A002
) -> Response:
    """The manual entry form in a project (``?issue=`` fills the issue)."""
    project = await _project_for(db, user, project_key)
    await _require_can_open_form(db, user, project)
    currency = await CurrencyService().reporting_currency_for(db, project)
    form = _blank_form(project, currency)
    form["issue"] = issue.strip().upper()[:50]
    back = _next_or(next, f"/projects/{project.key}/ai-usage/")
    return await _render_form(
        request,
        db,
        user,
        form=form,
        fixed_project=project,
        form_action=f"/projects/{project.key}/ai-usage/new/",
        back_url=back,
        next_url=back,
    )


@router.get(MY_URL + "new/", response_class=HTMLResponse)
async def my_ai_usage_form(
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: AsyncSession = DB_SESSION,
) -> Response:
    """The manual entry form for one's own usage, in any project one may log in or unassigned."""
    currency = await CurrencyService().reporting_currency_for(db, None)
    return await _render_form(
        request,
        db,
        user,
        form=_blank_form(None, currency),
        fixed_project=None,
        form_action=MY_URL + "new/",
        back_url=MY_URL,
        next_url=MY_URL,
    )


async def _create(
    request: Request,
    db: AsyncSession,
    user: User,
    form: dict[str, Any],
    *,
    fixed_project: Project | None,
    form_action: str,
    next_url: str,
) -> Response:
    try:
        if fixed_project is not None:
            form["project"] = fixed_project.key
        project, issue = await _target(db, user, form["project"], form["issue"])
        values = _usage_values(form)
        data = AiUsageInput(**values, issue_id=issue.id if issue else None, comments=form["comments"])
        async with db.begin_nested():
            await _service.create_for(
                db,
                user,
                data,
                project=project,
                on_behalf_of=form["on_behalf_of"] or None,
                source="manual",
                request=request,
            )
    except (AppError, _FormError) as exc:
        return await _render_form(
            request,
            db,
            user,
            form=form,
            fixed_project=fixed_project,
            form_action=form_action,
            back_url=next_url,
            next_url=next_url,
            error=_error_of(exc),
            status_code=422,
        )
    return _redirect(next_url, {"notice": "logged"})


_FORM_FIELDS = (
    "project",
    "issue",
    "on_behalf_of",
    "tool",
    "provider",
    "model",
    "usage_date",
    "request_count",
    "cost_type",
    "cost_amount",
    "cost_currency",
    "comments",
)


async def _submitted(request: Request) -> tuple[dict[str, Any], str]:
    """The entry form as posted (every field optional) and its ``next``."""
    data = await request.form()

    def text(name: str) -> str:
        value = data.get(name, "")
        return value if isinstance(value, str) else ""

    form = _read_form(
        **{name: text(name) for name in _FORM_FIELDS},
        tokens={c: text(f"tokens_{c}") for c in TOKEN_CLASSES},
    )
    return form, text("next")


@router.post("/projects/{project_key}/ai-usage/new/", response_model=None)
async def project_ai_usage_create(
    project_key: str,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: AsyncSession = DB_SESSION,
) -> Response:
    project = await _project_for(db, user, project_key)
    form, raw_next = await _submitted(request)
    next_url = _next_or(raw_next, f"/projects/{project.key}/ai-usage/")
    return await _create(
        request,
        db,
        user,
        form,
        fixed_project=project,
        form_action=f"/projects/{project.key}/ai-usage/new/",
        next_url=next_url,
    )


@router.post(MY_URL + "new/", response_model=None)
async def my_ai_usage_create(
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: AsyncSession = DB_SESSION,
) -> Response:
    form, raw_next = await _submitted(request)
    return await _create(
        request, db, user, form, fixed_project=None, form_action=MY_URL + "new/", next_url=_next_or(raw_next, MY_URL)
    )


async def _editable_entry(db: AsyncSession, user: User, entry_id: int) -> AiUsageEntry:
    """Entry *entry_id* if *user* may read it (else 404) and change it (else 403)."""
    entry = await _service.get_visible(db, entry_id, user)
    if not await _service.can_edit(db, user, entry):
        raise PermissionDeniedError("You do not have permission to edit this AI usage entry")
    return entry


async def _edit_context(db: AsyncSession, user: User, entry: AiUsageEntry) -> tuple[EntryReferences, dict[str, Any]]:
    refs = await _service.references(db, user, [entry])
    return refs, (await _rows(db, user, [entry], refs))[0]


@router.get("/ai-usage/{entry_id}/edit/", response_class=HTMLResponse)
async def ai_usage_edit_form(
    entry_id: int,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: AsyncSession = DB_SESSION,
    next: str = Query(""),  # noqa: A002
) -> Response:
    """Edit an entry: everything on manual, API and MCP entries; project, issue and comments on reported ones."""
    entry = await _editable_entry(db, user, entry_id)
    refs, row = await _edit_context(db, user, entry)
    currency = await CurrencyService().reporting_currency_for(db, None)
    project_key = refs.project_keys.get(entry.project_id or 0)
    fallback = MY_URL if entry.user_id == user.id or not project_key else f"/projects/{project_key}/ai-usage/"
    back = _next_or(next, fallback)
    return await _render_form(
        request,
        db,
        user,
        form=_form_of(entry, refs, currency),
        fixed_project=None,
        form_action=f"/ai-usage/{entry.id}/edit/",
        back_url=back,
        next_url=back,
        entry=entry,
        entry_row=row,
    )


@router.post("/ai-usage/{entry_id}/edit/", response_model=None)
async def ai_usage_update(
    entry_id: int,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: AsyncSession = DB_SESSION,
) -> Response:
    entry = await _editable_entry(db, user, entry_id)
    form, raw_next = await _submitted(request)
    next_url = _next_or(raw_next, MY_URL)
    try:
        project, issue = await _target(db, user, form["project"], form["issue"])
        changes: dict[str, Any] = {
            "project_id": project.id if project else None,
            "issue_id": issue.id if issue else None,
            "comments": form["comments"],
        }
        if not entry.is_locked:
            changes.update(_usage_values(form))
        async with db.begin_nested():
            await _service.update(db, user, entry, changes, request=request)
    except (AppError, _FormError) as exc:
        entry = await _service.get_visible(db, entry_id, user)
        _refs, row = await _edit_context(db, user, entry)
        return await _render_form(
            request,
            db,
            user,
            form=form,
            fixed_project=None,
            form_action=f"/ai-usage/{entry.id}/edit/",
            back_url=next_url,
            next_url=next_url,
            entry=entry,
            entry_row=row,
            error=_error_of(exc),
            status_code=422,
        )
    return _redirect(next_url, {"notice": "saved"})


# ---------------------------------------------------------------------------
# Row actions
# ---------------------------------------------------------------------------


async def _set_excluded(
    request: Request, db: AsyncSession, user: User, entry_id: int, excluded: bool, next_raw: str
) -> Response:
    entry = await _service.get_visible(db, entry_id, user)
    await _service.set_excluded(db, user, entry, excluded, request=request)
    return _redirect(_next_or(next_raw, MY_URL), {"notice": "excluded" if excluded else "included"})


@router.post("/ai-usage/{entry_id}/exclude/", response_model=None)
async def ai_usage_exclude(
    entry_id: int,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: AsyncSession = DB_SESSION,
    next: str = Form(""),  # noqa: A002
) -> Response:
    """Leave an entry out of totals and reports; a reporter's later updates keep it excluded."""
    return await _set_excluded(request, db, user, entry_id, True, next)


@router.post("/ai-usage/{entry_id}/include/", response_model=None)
async def ai_usage_include(
    entry_id: int,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: AsyncSession = DB_SESSION,
    next: str = Form(""),  # noqa: A002
) -> Response:
    """Count an excluded entry again."""
    return await _set_excluded(request, db, user, entry_id, False, next)


@router.post("/ai-usage/{entry_id}/delete/", response_model=None)
async def ai_usage_delete(
    entry_id: int,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: AsyncSession = DB_SESSION,
    next: str = Form(""),  # noqa: A002
) -> Response:
    """Delete an entry: manual, API and MCP entries by whoever may edit them; reported ones by administrators."""
    entry = await _service.get_visible(db, entry_id, user)
    await _service.delete(db, user, entry, request=request)
    return _redirect(_next_or(next, MY_URL), {"notice": "deleted"})
