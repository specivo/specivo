"""Web pages for AI spend reports: the project report, the admin "AI spend" page and its entry list.

- ``/projects/{key}/ai-usage/report/``: the project's AI usage the reader may
  see, grouped and totalled in one currency, with a chart.
- ``/admin/ai-spend/``: the whole instance (administrators), unallocated seat
  cost included.
- ``/admin/ai-spend/entries/``: the entries behind a cell of the admin
  report, across projects (drill-through); a project report drills through to
  the project's "AI usage" tab instead.

Every rule (visibility, seat allocation, who sees seat cost) is
:class:`AiSpendReportService`'s. The page is a plain GET form; the chart is
drawn by ``frontend/js/modules/spend-chart.js`` from a ``data-`` attribute,
and the table holds the same numbers for readers without it. The CSV export is
the REST endpoint with the same query.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import DB_SESSION
from specivo.core.exceptions import AppError, NotFoundError, PermissionDeniedError
from specivo.core.exchange import ConversionSummary
from specivo.core.i18n import get_locale
from specivo.core.i18n import gettext as _
from specivo.core.money import currency_display_name
from specivo.models.ai_usage_entry import AI_USAGE_COST_TYPES
from specivo.models.project import Project
from specivo.models.user import User
from specivo.services.ai_spend_report_service import (
    GROUP_BY,
    HIDDEN_ISSUE,
    PERIODS,
    PRESETS,
    AiSpendReportService,
    ReportFilters,
    ReportRow,
    SpendReport,
    default_range,
    period_end,
)
from specivo.services.ai_subscription_service import AiToolService
from specivo.services.ai_usage_service import AiUsageService
from specivo.services.currency_service import CurrencyService
from specivo.services.project_service import ProjectService
from specivo.web.deps import get_templates, require_user
from specivo.web.pages.admin import require_admin
from specivo.web.pages.ai_usage import NONE_VALUE, _filters, _fmt_int, _issue_filter, _money_view, _parse_date, _rows

router = APIRouter(tags=["web-ai-usage"], include_in_schema=False)

_reports = AiSpendReportService()
_usage = AiUsageService()
_projects = ProjectService()

ADMIN_URL = "/admin/ai-spend/"
ADMIN_ENTRIES_URL = "/admin/ai-spend/entries/"
#: Rows the chart draws at most (the table shows them all).
CHART_MAX_ROWS = 40


# ---------------------------------------------------------------------------
# The report form
# ---------------------------------------------------------------------------


@dataclass
class ReportForm:
    """The report parameters as typed, cleaned; unknown values fall back to the defaults."""

    preset: str = "30d"
    from_date: str = ""
    to_date: str = ""
    group: str = "user"
    group2: str = ""
    period: str = ""
    currency: str = ""
    policy: str = "entry_date"
    seats: str = "on"
    sort: str = "key"
    user_id: str = ""
    tool: str = ""
    model: str = ""
    cost_type: str = ""

    @classmethod
    def read(cls, **raw: str) -> ReportForm:
        form = cls(
            preset=raw.get("preset", "") if raw.get("preset", "") in PRESETS else "",
            from_date=raw.get("from_date", "") if _parse_date(raw.get("from_date")) else "",
            to_date=raw.get("to_date", "") if _parse_date(raw.get("to_date")) else "",
            group=raw.get("group", "user") if raw.get("group", "user") in (*GROUP_BY, "") else "user",
            group2=raw.get("group2", "") if raw.get("group2", "") in GROUP_BY else "",
            period=raw.get("period", "") if raw.get("period", "") in PERIODS else "",
            currency=raw.get("currency", "").strip().upper()[:3],
            policy=raw.get("policy", "") if raw.get("policy", "") in ("entry_date", "latest") else "entry_date",
            seats="off" if raw.get("seats") == "off" else "on",
            sort="spend" if raw.get("sort") == "spend" else "key",
            user_id=raw.get("user_id", "").strip() if raw.get("user_id", "").strip().isdigit() else "",
            tool=raw.get("tool", "").strip().lower()[:50],
            model=raw.get("model", "").strip()[:200],
            cost_type=raw.get("cost_type", "") if raw.get("cost_type", "") in AI_USAGE_COST_TYPES else "",
        )
        if form.group2 == form.group:
            form.group2 = ""
        if not form.preset:
            form.preset = "custom" if form.from_date or form.to_date else "30d"
        return form

    def dates(self) -> tuple[date, date]:
        if self.preset == "custom":
            first, last = default_range("30d")
            return _parse_date(self.from_date) or first, _parse_date(self.to_date) or last
        return default_range(self.preset)

    @property
    def group_by(self) -> list[str]:
        return [g for g in (self.group, self.group2) if g]

    def query(self, **override: str) -> str:
        values = {
            "preset": self.preset,
            "from_date": self.from_date if self.preset == "custom" else "",
            "to_date": self.to_date if self.preset == "custom" else "",
            "group": self.group,
            "group2": self.group2,
            "period": self.period,
            "currency": self.currency,
            "policy": self.policy if self.policy != "entry_date" else "",
            "seats": "off" if self.seats == "off" else "",
            "sort": self.sort if self.sort != "key" else "",
            "user_id": self.user_id,
            "tool": self.tool,
            "model": self.model,
            "cost_type": self.cost_type,
            **override,
        }
        return urlencode({k: v for k, v in values.items() if v})

    def api_query(self, report: SpendReport) -> str:
        """The REST query of the same report (for the CSV link)."""
        values = {
            "from_date": report.date_from.isoformat(),
            "to_date": report.date_to.isoformat(),
            "group_by": ",".join(report.group_by),
            "period": report.period or "",
            "currency": report.currency,
            "rate_policy": self.policy,
            "include_seat_cost": "false" if self.seats == "off" else "true",
            "sort": self.sort,
            "user_id": self.user_id,
            "tool": self.tool,
            "model": self.model,
            "cost_type": self.cost_type,
            "format": "csv",
        }
        return urlencode({k: v for k, v in values.items() if v != ""})


def _form_params(
    preset: str = Query(""),
    from_date: str = Query(""),
    to_date: str = Query(""),
    group: str = Query("user"),
    group2: str = Query(""),
    period: str = Query(""),
    currency: str = Query(""),
    policy: str = Query(""),
    seats: str = Query(""),
    sort: str = Query(""),
    user_id: str = Query(""),
    tool: str = Query(""),
    model: str = Query(""),
    cost_type: str = Query(""),
) -> ReportForm:
    return ReportForm.read(
        preset=preset,
        from_date=from_date,
        to_date=to_date,
        group=group,
        group2=group2,
        period=period,
        currency=currency,
        policy=policy,
        seats=seats,
        sort=sort,
        user_id=user_id,
        tool=tool,
        model=model,
        cost_type=cost_type,
    )


# ---------------------------------------------------------------------------
# View model
# ---------------------------------------------------------------------------


def _number(summary: ConversionSummary | None) -> float:
    """A converted total as a float, for the chart only (the table shows the exact amount)."""
    return float(summary.total.amount) if summary is not None else 0.0


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
            params[name] = value
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
        labels = {
            "subscription": _("Subscription"),
            "api": _("API (pay per use)"),
            "local": _("Local model"),
            "free": _("Free"),
        }
        return labels.get(cell["value"], _("Unknown"))
    return str(cell["label"])


def _rates_view(report: SpendReport) -> list[dict[str, str]]:
    """The rates used, one line per currency pair: the rate (or its range) and the dates it comes from."""
    pairs: dict[tuple[str, str], list[Any]] = {}
    for used in report.rates_used:
        pairs.setdefault((used.from_currency, used.to_currency), []).append(used)
    out = []
    for (src, dst), items in sorted(pairs.items()):
        rates = sorted({u.rate.normalize() for u in items})
        dates = sorted(u.rate_date for u in items if u.rate_date is not None)
        rate = f"{rates[0]:f}" if len(rates) == 1 else f"{rates[0]:f}–{rates[-1]:f}"
        span = ""
        if dates:
            span = (
                dates[0].isoformat() if dates[0] == dates[-1] else f"{dates[0].isoformat()} – {dates[-1].isoformat()}"
            )
        out.append({"from": src, "to": dst, "rate": rate[:30], "date": span, "count": str(len(dates))})
    return out


def _chart(report: SpendReport, views: list[dict[str, Any]]) -> dict[str, Any]:
    """The chart's data: spend per period bucket and group, or spend and API-equivalent value per group."""
    points = []
    for row, view in zip(report.rows[: CHART_MAX_ROWS * 10], views, strict=False):
        label = " · ".join(_cell_text(c) for c in view["cells"]) or _("All usage")
        points.append(
            {
                "period": view["period"],
                "label": label,
                "spend": _number(row.total_spend),
                "api": _number(row.api_equivalent),
            }
        )
    if not report.period:
        points = points[:CHART_MAX_ROWS]
    return {"currency": report.currency, "period": report.period or "", "points": points, "locale": get_locale()}


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
            chart=_chart(report, views),
            rate_dates=[d.isoformat() for d in report.rate_dates],
            rates_used=_rates_view(report),
        )
    return context


async def _run(
    db: AsyncSession, user: User, project: Project | None, form: ReportForm
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
            period=form.period or None,
            currency=form.currency or None,
            rate_policy=form.policy,
            include_seat_cost=form.seats != "off",
            filters=ReportFilters(
                user_id=int(form.user_id) if form.user_id else None,
                tool=form.tool or None,
                model=form.model or None,
                cost_type=form.cost_type or None,
            ),
            sort=form.sort,
        )
    except PermissionDeniedError:
        raise
    except AppError as exc:
        return None, {"code": exc.code, "field": exc.field or "", "message": exc.message}
    return report, {}


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


@router.get("/projects/{project_key}/ai-usage/report/", response_class=HTMLResponse)
async def project_spend_report(
    project_key: str,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    form: Annotated[ReportForm, Depends(_form_params)],
    db: AsyncSession = DB_SESSION,
) -> Response:
    """The project's AI spend report: rows from the entries the reader may see."""
    project = await _projects.get_by_key(db, project_key.upper())
    await _projects.require_project_access(db, project, user)
    report, error = await _run(db, user, project, form)
    members = await _projects.list_members(db, project) if report is not None and report.seat_cost_shown else []
    return get_templates().TemplateResponse(
        request,
        "pages/ai_usage/report.html",
        context={
            "user": user,
            "active_page": "ai_usage",
            "active_project": project,
            "project": project,
            "base_url": f"/projects/{project.key}/ai-usage/report/",
            "csv_base": f"/api/v1/projects/{project.key}/ai-usage/report/",
            "entries_url": f"/projects/{project.key}/ai-usage/",
            "members": [{"id": m["user_id"], "name": m["display_name"] or m["login"]} for m in members],
            "error": error,
            **await _context(db, report, form),
        },
        status_code=422 if error else 200,
    )


@router.get(ADMIN_URL, response_class=HTMLResponse)
async def admin_spend_report(
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    form: Annotated[ReportForm, Depends(_form_params)],
    db: AsyncSession = DB_SESSION,
) -> Response:
    """The instance's AI spend report, unallocated seat cost included."""
    from sqlalchemy import select

    report, error = await _run(db, user, None, form)
    people = (await db.execute(select(User.id, User.login, User.display_name).where(User.status == "active"))).all()
    return get_templates().TemplateResponse(
        request,
        "pages/ai_usage/report.html",
        context={
            "user": user,
            "active_page": "admin",
            "project": None,
            "base_url": ADMIN_URL,
            "csv_base": "/api/v1/ai-usage/report/",
            "entries_url": ADMIN_ENTRIES_URL,
            "members": sorted(
                ({"id": uid, "name": name or login} for uid, login, name in people), key=lambda m: m["name"].lower()
            ),
            "error": error,
            **await _context(db, report, form),
        },
        status_code=422 if error else 200,
    )


@router.get(ADMIN_ENTRIES_URL, response_class=HTMLResponse)
async def admin_spend_entries(
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
    user_id: str = Query(""),
    project: str = Query(""),
    issue: str = Query(""),
    tool: str = Query(""),
    model: str = Query(""),
    provider: str = Query(""),
    branch: str = Query(""),
    cost_type: str = Query(""),
    from_date: str = Query(""),
    to_date: str = Query(""),
    status: str = Query(""),
    offset: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=100),
) -> Response:
    """The entries behind a cell of the admin report, across projects (``project=-``: unassigned)."""
    filters = _filters(
        user_id=user_id,
        tool=tool,
        model=model,
        cost_type=cost_type,
        issue=issue,
        from_date=from_date,
        to_date=to_date,
        status=status,
        project=project,
        provider=provider,
        branch=branch,
    )
    selected: Project | None = None
    unassigned: bool | None = None
    if filters.project == NONE_VALUE:
        unassigned = True
    elif filters.project:
        try:
            selected = await _projects.get_by_key(db, filters.project)
        except NotFoundError:
            filters.project = ""
    issue_id = await _issue_filter(db, user, filters.issue, selected)
    entries, total = await _usage.list_entries(
        db,
        user,
        project=selected,
        user_id=int(filters.user_id) if filters.user_id else None,
        date_from=_parse_date(filters.from_date),
        date_to=_parse_date(filters.to_date),
        tool=filters.tool or None,
        model=filters.model or None,
        cost_type=filters.cost_type or None,
        issue_id=issue_id,
        without_issue=filters.issue == NONE_VALUE,
        excluded=filters.excluded,
        unassigned=unassigned,
        offset=offset,
        limit=limit,
        **filters.extra(),
    )
    refs = await _usage.references(db, user, entries)
    return get_templates().TemplateResponse(
        request,
        "pages/ai_usage/admin_entries.html",
        context={
            "user": user,
            "active_page": "admin",
            "rows": await _rows(db, user, entries, refs),
            "total": total,
            "offset": offset,
            "limit": limit,
            "filters": filters,
            "page_query": filters.query(),
            "return_to": request.url.path + (f"?{request.url.query}" if request.url.query else ""),
        },
    )
