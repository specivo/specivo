"""The spend report's view model: rows, rates, links, charts and the report run."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from urllib.parse import urlencode

from babel.numbers import format_compact_decimal
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import AppError, PermissionDeniedError
from specivo.core.i18n import get_locale
from specivo.core.i18n import gettext as _
from specivo.core.money import currency_display_name
from specivo.models.ai_subscription import AiSubscription
from specivo.models.ai_usage_entry import AI_USAGE_COST_TYPES, AiUsageEntry
from specivo.models.project import Project
from specivo.models.user import User
from specivo.services.ai_spend_report_service import (
    GROUP_BY,
    HIDDEN_ISSUE,
    PERIODS,
    PRESETS,
    ReportRow,
    SpendReport,
    period_end,
)
from specivo.services.ai_subscription_service import AiToolService
from specivo.services.ai_usage_chart_service import (
    BREAKDOWN_SHOWS,
    TOP_N,
    AiUsageChartService,
)
from specivo.services.currency_service import CurrencyService
from specivo.services.permission_service import ai_usage_visibility
from specivo.web.pages.ai_spend_reports._common import ADMIN_ENTRIES_URL, _reports
from specivo.web.pages.ai_spend_reports.form import SHOW_PARAMS, ReportForm, Scope, _cost_type_text
from specivo.web.pages.ai_usage._common import NONE_VALUE, _fmt_int
from specivo.web.pages.ai_usage.charts import _charts_view, cached_chart_payload, fold_breakdowns
from specivo.web.pages.ai_usage.rows import _money_view, _tool_names


def _drill(report: SpendReport, row: ReportRow, form: ReportForm) -> str:
    """The URL of the entries behind *row*: the project's AI usage tab, or the admin entry list."""
    if row.seat_only:
        user_id = row.key.get("user")
        return f"/admin/ai-subscriptions/?user_id={user_id}" if user_id is not None else "/admin/ai-subscriptions/"
    if row.key.get("issue") == HIDDEN_ISSUE:
        return ""
    first, last = report.date_from, report.date_to
    if row.period is not None and report.period is not None:
        first = max(first, row.period)
        last = min(last, period_end(row.period, report.period))
    params: dict[str, Any] = {"from_date": first.isoformat(), "to_date": last.isoformat()}
    key = row.key
    user_id = key.get("user") if "user" in key else (int(form.user_id) if form.user_id else None)
    if user_id is not None:
        params["user_id"] = user_id
    for name, value in (
        ("tool", key.get("tool", form.tool or None)),
        ("model", key.get("model", form.model or None)),
        ("cost_type", key.get("cost_type", form.cost_type or None)),
        ("provider", key.get("provider")),
    ):
        if value:
            params[name] = value  # noqa: PERF403
    if "issue" in key:
        params["issue"] = row.labels.get("issue") or NONE_VALUE
    if "branch" in key:
        params["branch"] = key["branch"] or NONE_VALUE
    params["status"] = "counted"
    if report.project is not None:
        return f"/projects/{report.project.key}/ai-usage/?" + urlencode(params)
    if "project" in key:
        params["project"] = row.labels.get("project") or NONE_VALUE
    return ADMIN_ENTRIES_URL + "?" + urlencode(params)


def _row_view(report: SpendReport, row: ReportRow, form: ReportForm, locale: str) -> dict[str, Any]:
    cells = []
    for dim in report.group_by:
        value = row.key.get(dim)
        kind = "value"
        if row.seat_only and value is None:
            kind = "seat"
        elif dim == "issue" and value == HIDDEN_ISSUE:
            kind = "hidden"
        elif value is None:
            kind = "none"
        cells.append({"dim": dim, "kind": kind, "label": row.labels.get(dim, ""), "value": value})
    return {
        "cells": cells,
        "period": row.period.isoformat() if row.period else "",
        "seat_only": row.seat_only,
        "entries": row.entries,
        "unpriced": row.unpriced_entries,
        "requests": _fmt_int(row.request_count, locale),
        "tokens": _fmt_int(row.total_tokens, locale),
        "tokens_short": _short_int(row.total_tokens, locale),
        "token_detail": {c: _fmt_int(n, locale) for c, n in row.tokens.items()},
        "marginal": _money_view(row.marginal, locale) if row.marginal else None,
        "api_equivalent": _money_view(row.api_equivalent, locale) if row.api_equivalent else None,
        "seat_allocated": _money_view(row.seat_allocated, locale) if row.seat_allocated else None,
        "seat_unallocated": _money_view(row.seat_unallocated, locale) if row.seat_unallocated else None,
        "has_unallocated": bool(
            row.seat_unallocated and (row.seat_unallocated.total.amount or row.seat_unallocated.unconverted)
        ),
        "total_spend": _money_view(row.total_spend, locale) if row.total_spend else None,
        "leverage": f"{row.leverage}×" if row.leverage is not None else "",
        "drill": _drill(report, row, form),
    }


def _short_int(value: int, locale: str) -> str:
    """A count in short form (``22.9B``, ``761.6M``) in the reader's locale; the full count goes in a title."""
    if abs(value) < 10_000:
        return _fmt_int(value, locale)
    try:
        return str(format_compact_decimal(value, format_type="short", locale=locale, fraction_digits=1))
    except (ValueError, LookupError):
        return _fmt_int(value, locale)


def _cell_text(cell: dict[str, Any]) -> str:
    """A row cell as plain text (chart labels), in the reader's language."""
    kind, dim = cell["kind"], cell["dim"]
    if kind == "seat":
        return _("Unallocated seat cost")
    if kind == "hidden":
        return _("An issue you cannot see")
    if kind == "none":
        return {"project": _("Unassigned"), "issue": _("No issue"), "branch": _("No branch")}.get(dim, "–")
    if dim == "cost_type":
        return _cost_type_text(cell["value"])
    return str(cell["label"])


#: Significant digits of a rate on the report page (the API and CSV keep the exact value).
RATE_DISPLAY_DIGITS = 6


def display_rate(rate: Decimal) -> str:
    """*rate* rounded to :data:`RATE_DISPLAY_DIGITS` significant digits, in plain notation.

    ``0.0298151460942158616577221228`` reads ``0.0298151``, ``32.4123`` reads
    ``32.4123``, ``1234.5678`` reads ``1234.57``; never an exponent.
    """
    if rate.is_zero() or not rate.is_finite():
        return f"{rate:f}"
    places = RATE_DISPLAY_DIGITS - 1 - rate.adjusted()
    rounded = (
        rate.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
        if places > 0
        else rate.quantize(Decimal(1), rounding=ROUND_HALF_UP)
    )
    text = f"{rounded:f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _rates_view(report: SpendReport) -> list[dict[str, str]]:
    """The rates used, one line per currency pair: the rate (or its range) and the dates it comes from."""
    pairs: dict[tuple[str, str], list[Any]] = {}
    for used in report.rates_used:
        pairs.setdefault((used.from_currency, used.to_currency), []).append(used)
    out = []
    for (src, dst), items in sorted(pairs.items()):
        rates = sorted({u.rate.normalize() for u in items})
        dates = sorted(u.rate_date for u in items if u.rate_date is not None)
        low, high = display_rate(rates[0]), display_rate(rates[-1])
        rate = low if low == high else f"{low}–{high}"
        span = ""
        if dates:
            span = (
                dates[0].isoformat() if dates[0] == dates[-1] else f"{dates[0].isoformat()} – {dates[-1].isoformat()}"
            )
        out.append({"from": src, "to": dst, "rate": rate, "date": span, "count": str(len(dates))})
    return out


def _bucket_links(base_url: str, form: ReportForm) -> list[dict[str, Any]]:
    """The charts' bucket switch: automatic, or a size the reader picks (the server still caps the points)."""
    out = []
    for value, label in (("", _("Auto")), ("day", _("Day")), ("week", _("Week")), ("month", _("Month"))):
        query = form.query(bucket=value)
        out.append(
            {
                "value": value,
                "label": label,
                "url": base_url + (f"?{query}" if query else ""),
                "active": value == form.bucket,
            }
        )
    return out


def _show_links(base_url: str, form: ReportForm) -> dict[str, list[dict[str, Any]]]:
    """Each breakdown chart's "Show" switch: how many groups it names before "Other" (a link per choice)."""
    shows = form.shows()
    out: dict[str, list[dict[str, Any]]] = {}
    for name, param in SHOW_PARAMS.items():
        links = []
        for n in BREAKDOWN_SHOWS:
            query = form.query(**{param: str(n) if n != TOP_N else ""})
            links.append({"value": n, "url": base_url + (f"?{query}" if query else ""), "active": n == shows[name]})
        out[name] = links
    return out


def _version_stmt(project: Project | None) -> Any:
    """What changes when the reported data does: the scope's entries and the plans (count and latest change)."""
    version = select(
        func.count(AiUsageEntry.id),
        func.max(AiUsageEntry.updated_at),
        select(func.count(AiSubscription.id)).scalar_subquery(),
        select(func.max(AiSubscription.updated_at)).scalar_subquery(),
    )
    if project is not None:
        version = version.where(AiUsageEntry.project_id == project.id)
    return version


async def _cache_parts(db: AsyncSession, user: User, project: Project | None, report: SpendReport) -> dict[str, Any]:
    """The cache key parts of a view of *report*: the scope, the reader and what they may see, every filter."""
    policy = report.rate_policy
    return {
        "scope": report.scope,
        "project": project.id if project is not None else None,
        "viewer": user.id,
        "admin": bool(user.is_admin),
        "visibility": await ai_usage_visibility(db, user, project) if project is not None else "",
        "seat_shown": report.seat_cost_shown,
        "seat_hidden": report.seat_cost_hidden,
        "currency": report.currency,
        "policy": [policy.kind.value, policy.max_age_days, policy.fixed_on],
        "from": report.date_from.isoformat(),
        "to": report.date_to.isoformat(),
        "filters": report.filters.cache_parts(),
        "seats": report.include_seat_cost,
    }


async def _charts(
    db: AsyncSession, user: User, project: Project | None, form: ReportForm, report: SpendReport
) -> dict[str, Any] | None:
    """The charts of the report, from Redis when the same view was built in the last minute.

    The cache key holds the scope, the reader, what the reader may see (their
    project visibility and whether seat cost is shown), every filter and the
    data version (the scope's entries and the plans: count and latest change),
    so two readers, or one reader whose rights changed, never share a payload.
    """

    async def build() -> dict[str, Any] | None:
        charts = await AiUsageChartService().report_charts(
            db,
            user,
            project=project,
            date_from=report.date_from,
            date_to=report.date_to,
            currency=report.currency,
            rate_policy=report.rate_policy,
            include_seat_cost=report.include_seat_cost,
            filters=report.filters,
            bucket=form.bucket or None,
            tool_names=await _tool_names(db),
        )
        return _charts_view(charts)

    parts = await _cache_parts(db, user, project, report)
    parts["bucket"] = form.bucket
    prefix = f"aiu:spend-charts:{report.scope}:{project.id if project is not None else 0}:{user.id}"
    # The payload keeps up to BREAKDOWN_MAX groups per breakdown; each chart shows the reader's choice.
    view = await cached_chart_payload(db, prefix, parts, _version_stmt(project), build)
    return fold_breakdowns(view, form.shows())


async def _context(db: AsyncSession, report: SpendReport | None, form: ReportForm) -> dict[str, Any]:
    locale = get_locale()
    currencies = await CurrencyService().list_currencies(db, active_only=True)
    tools = await AiToolService().list_tools(db, active_only=False)
    context: dict[str, Any] = {
        "form": form,
        "presets": PRESETS,
        "dimensions": GROUP_BY,
        "periods": PERIODS,
        "cost_types": list(AI_USAGE_COST_TYPES),
        "tools": [{"slug": t.slug, "name": t.name} for t in tools],
        "currencies": [
            {"code": c.code, "name": currency_display_name(c.code, locale, fallback=c.name)} for c in currencies
        ],
        "report": report,
    }
    if report is not None:
        views = [_row_view(report, r, form, locale) for r in report.rows]
        context.update(
            rows=views,
            totals=_row_view(report, report.totals, form, locale),
            rate_dates=[d.isoformat() for d in report.rate_dates],
            rates_used=_rates_view(report),
            unpriced_models=[{"model": m, "count": n} for m, n in report.unpriced_models],
        )
    return context


async def _run(
    db: AsyncSession, user: User, project: Project | None, form: ReportForm, scope: Scope
) -> tuple[SpendReport | None, dict[str, str]]:
    first, last = form.dates()
    try:
        report = await _reports.report(
            db,
            user,
            project=project,
            date_from=first,
            date_to=last,
            group_by=form.group_by,
            period=None if form.sessions else form.period or None,
            currency=form.currency or None,
            rate_policy=form.policy,
            include_seat_cost=form.seats != "off",
            filters=scope.filters,
            sort=form.sort,
        )
    except PermissionDeniedError:
        raise
    except AppError as exc:
        return None, {"code": exc.code, "field": exc.field or "", "message": exc.message}
    return report, {}


def _entries_url(report: SpendReport, form: ReportForm) -> str:
    """The entry list of the current level: the project's AI usage tab, or the admin entry list."""
    first, last = report.date_from, report.date_to
    params = {
        "from_date": first.isoformat(),
        "to_date": last.isoformat(),
        "user_id": form.user_id,
        "tool": form.tool,
        "model": form.model,
        "cost_type": form.cost_type,
        "provider": form.provider,
        "issue": form.issue,
        "branch": form.branch,
        "status": "counted",
    }
    if report.project is not None:
        return f"/projects/{report.project.key}/ai-usage/?" + urlencode({k: v for k, v in params.items() if v})
    params["project"] = form.project
    return ADMIN_ENTRIES_URL + "?" + urlencode({k: v for k, v in params.items() if v})
