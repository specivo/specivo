"""Web pages for AI spend reports: the project report, the admin "AI spend" page and its entry list.

- ``/projects/{key}/ai-usage/report/``: the project's AI usage the reader may
  see, grouped and totalled in one currency.
- ``/admin/ai-spend/``: the whole instance (administrators), unallocated seat
  cost included.
- ``/admin/ai-spend/entries/``: the entries behind a cell of the admin
  report, across projects; a project report links the project's "AI usage"
  tab instead.

Every rule (visibility, seat allocation, who sees seat cost) is
:class:`AiSpendReportService`'s. Both report pages are a drill-down whose
whole state is in the URL (:class:`ReportForm`): the grouping (``group``,
``sessions`` for the entries) and the drill filters (``project``,
``user_id``, ``issue``, ``tool``, ``model``, ``provider``, ``cost_type``,
``branch``). A row opens its group: its values become filters and the
grouping moves on (:data:`DRILL_NEXT`); the path above the title steps back.
The page renders every level on the server; htmx only swaps a level in
without a page load (``frontend/js/modules/spend-drill.js`` moves the focus).
Under the drill-down, "Spend by person" cards, then the AI usage charts
(``AiUsageChartService.report_charts``, the ``usage_charts`` macro), whose
stack colours the table swatches and cards share (``color_slots``). Charts
and cards are cached per reader (:func:`cached_chart_payload`). The CSV
export is the REST endpoint with the same query.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any
from urllib.parse import urlencode

from babel.numbers import format_compact_decimal
from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.ai_report_limits import enforce_ai_report_rate_limit
from specivo.core.database import DB_SESSION
from specivo.core.exceptions import AppError, NotFoundError, PermissionDeniedError
from specivo.core.i18n import get_locale, ngettext
from specivo.core.i18n import gettext as _
from specivo.core.money import currency_display_name
from specivo.models.ai_subscription import AiSubscription
from specivo.models.ai_usage_entry import AI_USAGE_COST_TYPES, AiUsageEntry
from specivo.models.issue import Issue
from specivo.models.project import Project
from specivo.models.user import User
from specivo.services.ai_spend_report_service import (
    GROUP_BY,
    HIDDEN_ISSUE,
    PERIODS,
    PRESETS,
    AiSpendReportService,
    DrillFacts,
    PersonUsage,
    ReportFilters,
    ReportRow,
    SpendReport,
    default_range,
    fold_rows,
    normalize_filter_provider,
    period_end,
)
from specivo.services.ai_subscription_service import AiToolService
from specivo.services.ai_usage_chart_service import (
    BREAKDOWN_SHOWS,
    CHOSEN_SIZES,
    NEUTRAL_SLOT,
    NO_PROJECT,
    TOP_N,
    AiUsageChartService,
    color_slots,
)
from specivo.services.ai_usage_service import AiUsageService
from specivo.services.currency_service import CurrencyService
from specivo.services.issue_service import IssueService
from specivo.services.permission_service import ai_usage_visibility
from specivo.services.project_service import ProjectService
from specivo.web.deps import get_templates, require_user
from specivo.web.pages.admin import require_admin
from specivo.web.pages.ai_usage import (
    BREAKDOWNS,
    NONE_VALUE,
    _charts_view,
    _filters,
    _fmt_int,
    _issue_filter,
    _money_view,
    _parse_date,
    _rows,
    _tool_names,
    cached_chart_payload,
    fold_breakdowns,
    person_filter_chip,
    sees_everyone,
)

router = APIRouter(tags=["web-ai-usage"], include_in_schema=False)

_reports = AiSpendReportService()
_usage = AiUsageService()
_projects = ProjectService()
_issues = IssueService()

ADMIN_URL = "/admin/ai-spend/"
ADMIN_ENTRIES_URL = "/admin/ai-spend/entries/"
#: Rows the chart draws at most (the table shows them all).
CHART_MAX_ROWS = 40
#: The query parameter of each breakdown chart's "Show" choice: show_user, show_project, ...
SHOW_PARAMS = {name: f"show_{name}" for name in BREAKDOWNS}
#: A query override that leaves every chart's "Show" choice out (cache keys that do not depend on it).
NO_SHOWS = dict.fromkeys(SHOW_PARAMS.values(), "")


# ---------------------------------------------------------------------------
# The report form
# ---------------------------------------------------------------------------

#: The "Sessions" level of the drill-down: the entries themselves, a page at a time.
SESSIONS = "sessions"
#: Drill filters in breadcrumb order, and the form field of each.
DRILL_ORDER: tuple[str, ...] = ("project", "user", "issue", "tool", "model", "provider", "cost_type", "branch")
FILTER_FIELD = {
    "project": "project",
    "user": "user_id",
    "issue": "issue",
    "tool": "tool",
    "model": "model",
    "provider": "provider",
    "cost_type": "cost_type",
    "branch": "branch",
}
#: Where a click on a row of each grouping leads: the first of these not filtered yet, else the sessions.
DRILL_NEXT: dict[str, tuple[str, ...]] = {
    "project": ("user", "issue"),
    "user": ("issue",),
    "issue": (),
    "tool": ("user", "issue"),
    "model": ("user", "issue"),
    "provider": ("model", "user", "issue"),
    "cost_type": ("user", "issue"),
    "branch": (),
}
#: Rows of a drill-down table; the rest are summed into one "other" row (the CSV has them all).
DRILL_MAX_ROWS = 30
#: Groups a drill-down table with a period ("Per week") shows in each period, the largest by
#: spend over the whole range; the rest are summed into one "other" row per period.
DRILL_PERIOD_GROUPS = 10
#: Bars of the "Spend by ..." card.
SIDE_BARS = 8
#: Person cards: the largest by spend; one more card sums the rest and opens the Person level.
PEOPLE_TOP = 10
#: Page sizes of the sessions level.
SESSION_LIMITS = (10, 25, 50)


def next_group(dim: str, filtered: set[str], instance: bool) -> str:
    """The grouping after a click on a *dim* row, *filtered* holding every drill filter then set."""
    for option in DRILL_NEXT.get(dim, ()):
        if option not in filtered and (instance or option != "project"):
            return option
    return SESSIONS


@dataclass
class ReportForm:
    """The report parameters as typed, cleaned; unknown values fall back to the defaults.

    Besides the report's own filters, the drill-down state lives here (and so
    in the URL): ``project`` (instance report: a key, ``-`` for unassigned),
    ``issue`` (a key, ``-`` for none), ``branch`` (``-`` for none),
    ``provider``, the grouping ``group`` (``sessions``: the entries), the
    sessions page (``offset``, ``limit``) and ``detail`` (every amount column).
    """

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
    #: A login typed into the person filter without JS; the page resolves it into ``user_id``.
    user: str = ""
    tool: str = ""
    model: str = ""
    cost_type: str = ""
    #: Time bucket of the charts ("" automatic); only a wish, the chart service caps the points.
    bucket: str = ""
    project: str = ""
    issue: str = ""
    branch: str = ""
    provider: str = ""
    offset: int = 0
    limit: int = 25
    detail: str = ""
    #: Groups each breakdown chart shows by name (``BREAKDOWN_SHOWS``), by chart (``BREAKDOWNS``).
    show: dict[str, int] = field(default_factory=dict)
    #: No grouping was asked for: the page picks its own (project on the instance report, user on a project's).
    group_missing: bool = False

    @classmethod
    def read(cls, **raw: str | None) -> ReportForm:
        def text(name: str) -> str:
            return (raw.get(name) or "").strip()

        group_raw = raw.get("group")
        group = (group_raw or "").strip()
        offset, limit = text("offset"), text("limit")
        form = cls(
            preset=text("preset") if text("preset") in PRESETS else "",
            from_date=text("from_date") if _parse_date(text("from_date")) else "",
            to_date=text("to_date") if _parse_date(text("to_date")) else "",
            group=group if group in (*GROUP_BY, SESSIONS, "") else "user",
            group2=text("group2") if text("group2") in GROUP_BY else "",
            period=text("period") if text("period") in PERIODS else "",
            currency=text("currency").upper()[:3],
            policy=text("policy") if text("policy") in ("entry_date", "latest") else "entry_date",
            seats="off" if text("seats") == "off" else "on",
            sort="spend" if text("sort") == "spend" else "key",
            user_id=text("user_id") if text("user_id").isdigit() else "",
            user=text("user")[:255],
            tool=text("tool").lower()[:50],
            model=text("model")[:200],
            cost_type=text("cost_type") if text("cost_type") in AI_USAGE_COST_TYPES else "",
            bucket=text("bucket") if text("bucket") in CHOSEN_SIZES else "",
            project=text("project").upper()[:50],
            issue=text("issue").upper()[:50],
            branch=text("branch")[:255],
            provider=text("provider").lower()[:50],
            offset=int(offset) if offset.isdigit() and int(offset) < 10_000_000 else 0,
            limit=int(limit) if limit.isdigit() and int(limit) in SESSION_LIMITS else 25,
            detail="1" if text("detail") == "1" else "",
            show={
                name: int(text(param))
                for name, param in SHOW_PARAMS.items()
                if text(param).isdigit() and int(text(param)) in BREAKDOWN_SHOWS
            },
            group_missing=group_raw is None,
        )
        if form.group2 == form.group or form.group == SESSIONS:
            form.group2 = ""
        if not form.preset:
            form.preset = "custom" if form.from_date or form.to_date else "30d"
        return form

    def for_scope(self, instance: bool) -> ReportForm:
        """Apply the scope: a project report has no project filter; the default grouping depends on the scope."""
        if not instance:
            self.project = ""
        if self.group_missing:
            self.group = "project" if instance else "user"
        return self

    def dates(self) -> tuple[date, date]:
        if self.preset == "custom":
            first, last = default_range("30d")
            return _parse_date(self.from_date) or first, _parse_date(self.to_date) or last
        return default_range(self.preset)

    @property
    def sessions(self) -> bool:
        return self.group == SESSIONS

    @property
    def group_by(self) -> list[str]:
        """The report's grouping; the sessions level groups by model (its "Spend by model" card)."""
        if self.sessions:
            return ["model"]
        return [g for g in (self.group, self.group2) if g]

    def filtered(self) -> list[str]:
        """The drill filters set, in breadcrumb order."""
        return [d for d in DRILL_ORDER if getattr(self, FILTER_FIELD[d])]

    def query(self, **override: Any) -> str:
        values: dict[str, Any] = {
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
            "project": self.project,
            "user_id": self.user_id,
            "issue": self.issue,
            "tool": self.tool,
            "model": self.model,
            "provider": self.provider,
            "cost_type": self.cost_type,
            "branch": self.branch,
            "bucket": self.bucket,
            "detail": self.detail,
            "offset": str(self.offset) if self.offset else "",
            "limit": str(self.limit) if self.limit != 25 else "",
            **{param: str(n) for name, param in SHOW_PARAMS.items() if (n := self.show.get(name, TOP_N)) != TOP_N},
            **override,
        }
        # An unknown login typed into the person filter stays in the links, to be named again.
        values["user"] = self.user if values.get("user_id") == "0" else ""
        if values.get("group") != SESSIONS:
            values["offset"] = values["limit"] = ""
        return urlencode({k: v for k, v in values.items() if v})

    def shows(self) -> dict[str, int]:
        """Groups each breakdown chart shows by name: the reader's choice, else ``TOP_N``."""
        return {name: self.show.get(name, TOP_N) for name in BREAKDOWNS}

    def level_query(self, group: str, **override: Any) -> str:
        """The query of another level: *group*, without "then by", period bucket or page."""
        return self.query(group=group, group2="", period="", offset="", limit="", **override)

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
            "project": self.project if report.project is None else "",
            "user_id": self.user_id,
            "issue": self.issue,
            "tool": self.tool,
            "model": self.model,
            "provider": self.provider,
            "cost_type": self.cost_type,
            "branch": self.branch,
            "format": "csv",
        }
        return urlencode({k: v for k, v in values.items() if v != ""})


def _form_params(
    preset: str = Query(""),
    from_date: str = Query(""),
    to_date: str = Query(""),
    group: str | None = Query(None),
    group2: str = Query(""),
    period: str = Query(""),
    currency: str = Query(""),
    policy: str = Query(""),
    seats: str = Query(""),
    sort: str = Query(""),
    user_id: str = Query(""),
    user: str = Query(""),
    tool: str = Query(""),
    model: str = Query(""),
    cost_type: str = Query(""),
    bucket: str = Query(""),
    project: str = Query(""),
    issue: str = Query(""),
    branch: str = Query(""),
    provider: str = Query(""),
    offset: str = Query(""),
    limit: str = Query(""),
    detail: str = Query(""),
    show_user: str = Query(""),
    show_project: str = Query(""),
    show_model: str = Query(""),
    show_tool: str = Query(""),
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
        user=user,
        tool=tool,
        model=model,
        cost_type=cost_type,
        bucket=bucket,
        project=project,
        issue=issue,
        branch=branch,
        provider=provider,
        offset=offset,
        limit=limit,
        detail=detail,
        show_user=show_user,
        show_project=show_project,
        show_model=show_model,
        show_tool=show_tool,
    )


@dataclass
class Scope:
    """The drill filters resolved: what the report counts, and how to name each filter."""

    filters: ReportFilters
    #: The project of the project filter (instance report), for the entry list.
    selected: Project | None = None
    #: Breadcrumb label per drill filter.
    labels: dict[str, str] = field(default_factory=dict)
    #: The subject of the issue filter, when the reader may see it.
    issue_subject: str = ""


async def _resolve(db: AsyncSession, user: User, project: Project | None, form: ReportForm) -> Scope:
    """The report filters of *form*: project and issue keys resolved, within what *user* may see.

    An unknown project, or an issue the reader cannot see (or of another
    project on a project report), matches nothing rather than being ignored,
    so a shared link never shows more than it says.
    """
    scope = Scope(filters=ReportFilters())
    project_id: int | None = None
    unassigned = False
    if project is None and form.project:
        if form.project == NONE_VALUE:
            unassigned = True
            scope.labels["project"] = _("Unassigned")
        else:
            try:
                scope.selected = await _projects.get_by_key(db, form.project)
                project_id = scope.selected.id
            except NotFoundError:
                project_id = -1
            scope.labels["project"] = form.project
    issue_id: int | None = None
    if form.issue == NONE_VALUE:
        scope.labels["issue"] = _("No issue")
    elif form.issue:
        issue_id = -1
        try:
            found = await _issues.get_by_display_key(db, form.issue, user=user)
        except NotFoundError:
            found = None
        within = project or scope.selected
        if found is not None and (within is None or found.project_id == within.id):
            issue_id = found.id
            scope.issue_subject = found.subject
        scope.labels["issue"] = form.issue
    if form.user_id:
        person = await db.get(User, int(form.user_id))
        named = (person.display_name or person.login) if person is not None else ""
        scope.labels["user"] = named or form.user or form.user_id
    if form.tool:
        scope.labels["tool"] = (await _tool_names(db)).get(form.tool, form.tool)
    if form.model:
        scope.labels["model"] = form.model
    if form.provider:
        scope.labels["provider"] = form.provider
    if form.cost_type:
        scope.labels["cost_type"] = _cost_type_text(form.cost_type)
    if form.branch:
        scope.labels["branch"] = _("No branch") if form.branch == NONE_VALUE else form.branch
    scope.filters = ReportFilters(
        user_id=int(form.user_id) if form.user_id else None,
        tool=form.tool or None,
        model=form.model or None,
        provider=normalize_filter_provider(form.provider),
        cost_type=form.cost_type or None,
        project_id=project_id,
        unassigned=unassigned,
        issue_id=issue_id,
        without_issue=form.issue == NONE_VALUE,
        branch=form.branch if form.branch and form.branch != NONE_VALUE else None,
        without_branch=form.branch == NONE_VALUE,
    )
    return scope


def _cost_type_text(value: str) -> str:
    return {
        "subscription": _("Subscription"),
        "api": _("API (pay per use)"),
        "local": _("Local model"),
        "free": _("Free"),
    }.get(value, _("Unknown"))


# ---------------------------------------------------------------------------
# View model
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Drill-down
# ---------------------------------------------------------------------------


def _spend(row: ReportRow) -> Decimal:
    return row.total_spend.total.amount if row.total_spend is not None else Decimal(0)


def _chart_key(dim: str, value: Any) -> str:
    """A row key as the chart stacks name it (``ai_usage_chart_service``)."""
    if dim == "project" and value is None:
        return NO_PROJECT
    return "" if value is None else str(value)


def _slot_map(dim: str, charts: dict[str, Any] | None, rows: list[ReportRow]) -> tuple[dict[str, int], bool]:
    """Colour slots by chart key: the chart's on the page when it stacks *dim*, else by spend rank.

    Returns ``(slots, from_chart)``; with a chart, groups it folded into
    "other" are grey there and here.
    """
    stacks = ((charts or {}).get("time") or {}).get("stacks") or {}
    stack = stacks.get(dim)
    if stack and stack.get("keys") and stack.get("colors"):
        return {str(k): int(c) for k, c in zip(stack["keys"], stack["colors"], strict=False)}, True
    ranked = sorted(rows, key=lambda r: -_spend(r))
    return color_slots(_chart_key(dim, r.key.get(dim)) for r in ranked), False


def _slot(slots: tuple[dict[str, int], bool], dim: str, value: Any) -> int:
    return slots[0].get(_chart_key(dim, value), NEUTRAL_SLOT)


def _share(part: Decimal, whole: Decimal) -> dict[str, Any]:
    """A share of the total as a bar width (0-100) and a label."""
    if whole <= 0 or part <= 0:
        return {"width": 0, "label": "–" if whole <= 0 else "0%"}
    pct = float(part / whole * 100)
    label = "<1%" if pct < 1 else f"{round(pct)}%"
    return {"width": round(min(pct, 100.0), 1), "label": label}


def _row_dims(report: SpendReport, row: ReportRow) -> dict[str, str] | None:
    """The drill filter values of *row*, or None when it cannot be opened (a fee, a hidden issue, no provider)."""
    if row.seat_only:
        return None
    values: dict[str, str] = {}
    for dim in report.group_by:
        value = row.key.get(dim)
        if dim == "user":
            if value is None:
                return None
            values["user_id"] = str(value)
        elif dim == "project":
            values["project"] = row.labels.get("project") or NONE_VALUE if value is not None else NONE_VALUE
        elif dim == "issue":
            if value == HIDDEN_ISSUE:
                return None
            values["issue"] = row.labels.get("issue") or NONE_VALUE if value is not None else NONE_VALUE
        elif dim == "branch":
            values["branch"] = value or NONE_VALUE
        elif value is None or value == "":
            return None
        else:
            values[FILTER_FIELD[dim]] = str(value)
    return values


def _open_url(base_url: str, report: SpendReport, row: ReportRow, form: ReportForm) -> str:
    """The drill-down URL of *row*: its values as filters, the next grouping, its period's dates."""
    values = _row_dims(report, row)
    if values is None or not report.group_by:
        return ""
    instance = report.project is None
    filtered = set(form.filtered()) | set(report.group_by)
    group = next_group(report.group_by[-1], filtered, instance)
    if group == "project" and not instance:
        group = SESSIONS
    if row.period is not None and report.period is not None:
        first = max(report.date_from, row.period)
        last = min(report.date_to, period_end(row.period, report.period))
        values.update(preset="custom", from_date=first.isoformat(), to_date=last.isoformat())
    return base_url + "?" + form.level_query(group, **values)


def _crumbs(base_url: str, form: ReportForm, scope: Scope, instance: bool, root: str) -> list[dict[str, Any]]:
    """The drill path: the whole report, then each filter in breadcrumb order; each opens its own level."""
    active = form.filtered()
    root_group = "project" if instance else "user"
    out: list[dict[str, Any]] = [
        {
            "dim": "",
            "label": root,
            "url": base_url + "?" + form.level_query(root_group, **{FILTER_FIELD[d]: "" for d in active}),
        }
    ]
    for i, dim in enumerate(active):
        cleared = {FILTER_FIELD[d]: "" for d in active[i + 1 :]}
        group = next_group(dim, set(active[: i + 1]), instance)
        out.append(
            {
                "dim": dim,
                "label": scope.labels.get(dim) or getattr(form, FILTER_FIELD[dim]),
                "url": base_url + "?" + form.level_query(group, **cleared),
            }
        )
    out[-1]["current"] = True
    return out


def _tabs(base_url: str, form: ReportForm, instance: bool) -> list[dict[str, Any]]:
    """The group-by switch: project (instance only), person, feature, tool, model and the sessions."""
    filtered = set(form.filtered())
    dims = (["project"] if instance else []) + ["user", "issue", "tool", "model", SESSIONS]
    return [
        {
            "group": dim,
            "url": base_url + "?" + form.level_query(dim),
            "active": form.group == dim,
            "disabled": dim in filtered,
        }
        for dim in dims
    ]


@dataclass
class Folded:
    """The rows a drill-down table shows, and what it sums into "other" rows."""

    #: The rows shown, in table order.
    rows: list[ReportRow]
    #: Without a period: one row summing the smaller groups.
    other: ReportRow | None = None
    #: The groups summed into "other" (with a period: over the whole range).
    groups: int = 0
    #: With a period: per period, the row summing the smaller groups there and how many it sums.
    period_other: dict[date, tuple[ReportRow, int]] = field(default_factory=dict)


def _group_of(report: SpendReport, row: ReportRow) -> tuple[Any, ...]:
    return (row.seat_only, *(row.key.get(d) for d in report.group_by))


def _fold(report: SpendReport) -> Folded:
    """The rows a drill-down table shows: all of them, or the largest by spend and "other" rows.

    Without a period, at most :data:`DRILL_MAX_ROWS` rows: the largest and
    one "other" row; the kept rows keep the report's order. With one (a row
    per period and group), the :data:`DRILL_PERIOD_GROUPS` groups with the
    largest spend over the whole range, in each period, and one "other" row
    per period summing the rest there; rows go by period, then the report's
    order. Every amount stays exact: an "other" row is the sum of its rows.
    """
    rows = report.rows
    if report.period:
        return _fold_periods(report)
    if len(rows) <= DRILL_MAX_ROWS:
        return Folded(rows=rows)
    ranked = sorted(range(len(rows)), key=lambda i: -_spend(rows[i]))
    keep = set(ranked[: DRILL_MAX_ROWS - 1])
    kept = [r for i, r in enumerate(rows) if i in keep]
    rest = [r for i, r in enumerate(rows) if i not in keep]
    return Folded(rows=kept, other=fold_rows(rest, report.currency, report.seat_cost_shown), groups=len(rest))


def _fold_periods(report: SpendReport) -> Folded:
    totals: dict[tuple[Any, ...], Decimal] = {}
    for row in report.rows:
        group = _group_of(report, row)
        totals[group] = totals.get(group, Decimal(0)) + _spend(row)
    # An "other" group of one is no help.
    if len(totals) <= DRILL_PERIOD_GROUPS + 1:
        return Folded(rows=report.rows)
    keep = set(sorted(totals, key=lambda g: -totals[g])[:DRILL_PERIOD_GROUPS])
    kept: list[ReportRow] = []
    rest: dict[date, list[ReportRow]] = {}
    for row in report.rows:
        if _group_of(report, row) in keep:
            kept.append(row)
        else:
            rest.setdefault(row.period or date.min, []).append(row)
    period_other: dict[date, tuple[ReportRow, int]] = {}
    for period, items in rest.items():
        other = fold_rows(items, report.currency, report.seat_cost_shown)
        other.period = period
        period_other[period] = (other, len({_group_of(report, r) for r in items}))
    kept.sort(key=lambda r: r.period or date.min)
    return Folded(rows=kept, groups=len(totals) - len(keep), period_other=period_other)


def _plural_people(count: int) -> str:
    return ngettext("%(num)d person", "%(num)d people", count) % {"num": count}


def _sub_line(
    dim: str,
    row: ReportRow,
    facts: DrillFacts,
    tool_names: dict[str, str],
    subjects: dict[int, str],
) -> str:
    """The line under a group's name: who used it (people), or what a person used (tools)."""
    value = row.key.get(dim)
    if row.seat_only:
        return _("Fees of subscriptions without priced usage")
    if dim == "user":
        usage = facts.usage.get(value) if value is not None else None
        return ", ".join(tool_names.get(t, t) for t in usage.tools) if usage else ""
    if dim == "issue" and value == HIDDEN_ISSUE:
        return ""
    people = facts.people_by_key.get(value)
    parts = []
    if dim == "issue" and value and subjects.get(value):
        parts.append(subjects[value])
    if people:
        parts.append(_plural_people(people))
    return " · ".join(parts)


async def _issue_subjects(db: AsyncSession, rows: list[ReportRow]) -> dict[int, str]:
    """Subjects of the issues the rows name (only issues the reader may see carry a label)."""
    ids = [r.key["issue"] for r in rows if r.key.get("issue") and r.labels.get("issue")]
    if not ids:
        return {}
    stmt = select(Issue.id, Issue.subject).where(Issue.id.in_(ids))
    return {iid: subject for iid, subject in (await db.execute(stmt)).all()}


def _side_card(
    title_dim: str, views: list[dict[str, Any]], other: dict[str, Any] | None, total: Decimal
) -> dict[str, Any]:
    """The "Spend by ..." card: the first rows of the table as bars, and one line saying what they show."""
    bars = views[:SIDE_BARS]
    rest = views[SIDE_BARS:]
    top = max(views, key=lambda v: v["spend_value"], default=None)
    largest = max((v["spend_value"] for v in bars), default=Decimal(0))
    items = [
        {
            "label": v["name"],
            "slot": v["slot"],
            "amount": v["spend_text"],
            "width": _share(v["spend_value"], largest)["width"],
        }
        for v in bars
    ]
    groups = len(views) + (other["groups"] if other else 0)
    summary = ngettext("%(num)d group.", "%(num)d groups.", groups) % {"num": groups}
    if top is not None and top["spend_value"] > 0:
        summary += " " + _("Largest: %(name)s with %(amount)s (%(share)s).") % {
            "name": top["name"],
            "amount": top["spend_text"],
            "share": _share(top["spend_value"], total)["label"],
        }
    if rest or other:
        hidden = len(rest) + (other["groups"] if other else 0)
        summary += " " + ngettext("%(num)d more in the table.", "%(num)d more in the table.", hidden) % {"num": hidden}
    return {"dim": title_dim, "bars": items, "summary": summary}


async def _drill_view(
    db: AsyncSession,
    user: User,
    project: Project | None,
    form: ReportForm,
    report: SpendReport,
    scope: Scope,
    facts: DrillFacts,
    charts: dict[str, Any] | None,
    base_url: str,
    folded: Folded,
) -> dict[str, Any]:
    """The drill-down: path, title, tiles, the group switch, the table (or the entries) and the side card."""
    locale = get_locale()
    instance = project is None
    tool_names = await _tool_names(db)
    root = _("All projects") if instance else project.name if project is not None else ""
    crumbs = _crumbs(base_url, form, scope, instance, root)
    title = crumbs[-1]["label"]
    if form.issue and scope.issue_subject:
        title = f"{form.issue} · {scope.issue_subject}"
    total_spend = _spend(report.totals)
    view: dict[str, Any] = {
        "crumbs": crumbs,
        "title": title,
        "tabs": _tabs(base_url, form, instance),
        "people": facts.people,
        "level": form.group if form.group in (*GROUP_BY, SESSIONS) else "",
        "single": len(report.group_by) == 1 and not report.period,
    }
    dim = report.group_by[0] if report.group_by else ""
    slots = _slot_map(dim, charts, report.rows) if dim else ({}, False)
    shown, other_row = folded.rows, folded.other
    subjects = await _issue_subjects(db, shown) if dim == "issue" else {}

    def money_text(row: ReportRow) -> str:
        return row.total_spend.total.format(locale) if row.total_spend is not None else "–"

    views = []
    for row in shown:
        item = _row_view(report, row, form, locale)
        name = _cell_text(item["cells"][0]) if item["cells"] else _("All usage")
        item.update(
            name=name,
            slot=_slot(slots, dim, row.key.get(dim)) if dim else NEUTRAL_SLOT,
            sub=_sub_line(dim, row, facts, tool_names, subjects) if view["single"] and dim else "",
            share=_share(_spend(row), total_spend),
            spend_value=_spend(row),
            spend_text=money_text(row),
            open=_open_url(base_url, report, row, form),
        )
        views.append(item)

    def other_view(row: ReportRow, groups: int) -> dict[str, Any]:
        item = _row_view(report, row, form, locale)
        item.update(
            is_other=True,
            groups=groups,
            share=_share(_spend(row), total_spend),
            spend_value=_spend(row),
            spend_text=money_text(row),
        )
        return item

    other = other_view(other_row, folded.groups) if other_row is not None else None
    period_note = None
    if folded.period_other:
        # Each period: its rows of the largest groups, then its "other" row.
        by_period: dict[date, list[dict[str, Any]]] = {}
        for row, item in zip(shown, views, strict=True):
            by_period.setdefault(row.period or date.min, []).append(item)
        for period, (row, groups) in folded.period_other.items():
            by_period.setdefault(period, []).append(other_view(row, groups))
        views = [item for period in sorted(by_period) for item in by_period[period]]
        period_note = {"shown": DRILL_PERIOD_GROUPS, "folded": folded.groups}
    view.update(rows=views, other=other, period_note=period_note)
    if form.sessions:
        view["side"] = _side_card("model", views, other, total_spend)
        view["side"]["summary"] = _sessions_summary(report, facts)
    elif view["single"] and dim:
        view["side"] = _side_card(dim, views, other, total_spend)
    else:
        view["side"] = None
    return view


def _sessions_summary(report: SpendReport, facts: DrillFacts) -> str:
    entries = ngettext("%(num)d entry", "%(num)d entries", report.totals.entries) % {"num": report.totals.entries}
    days = ngettext("%(num)d day", "%(num)d days", facts.days) % {"num": facts.days}
    models = ngettext("%(num)d model", "%(num)d models", len(report.rows)) % {"num": len(report.rows)}
    return _("%(entries)s over %(days)s, %(models)s.") % {"entries": entries, "days": days, "models": models}


async def _sessions(
    db: AsyncSession,
    user: User,
    project: Project | None,
    form: ReportForm,
    report: SpendReport,
    scope: Scope,
    charts: dict[str, Any] | None,
    request: Request,
) -> dict[str, Any]:
    """A page of the entries behind the current level, each with its spend (seat share included where shown)."""
    f = scope.filters
    entries: list[AiUsageEntry] = []
    total = 0
    if f.project_id != -1:
        entries, total = await _usage.list_entries(
            db,
            user,
            project=project or scope.selected,
            user_id=f.user_id,
            date_from=report.date_from,
            date_to=report.date_to,
            tool=f.tool,
            model=f.model,
            cost_type=f.cost_type,
            issue_id=f.issue_id,
            without_issue=f.without_issue,
            provider=f.provider,
            branch=f.branch,
            without_branch=f.without_branch,
            excluded=False,
            unassigned=True if f.unassigned else None,
            offset=form.offset,
            limit=form.limit,
        )
    refs = await _usage.references(db, user, entries)
    return_to = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    rows = await _rows(db, user, entries, refs)
    spend = await _reports.entry_spend(
        db,
        user,
        project=project,
        date_from=report.date_from,
        date_to=report.date_to,
        entry_ids=[e.id for e in entries],
        currency=report.currency,
        rate_policy=report.rate_policy,
        include_seat_cost=report.include_seat_cost,
    )
    locale = get_locale()
    user_slots = _slot_map("user", charts, [])
    for entry, row in zip(entries, rows, strict=True):
        priced = spend.get(entry.id)
        row["spend"] = priced.total_spend.total.format(locale) if priced and priced.total_spend else "–"
        row["slot"] = _slot(user_slots, "user", entry.user_id)
        row["tokens_short"] = _short_int(sum(entry.all_tokens().values()), locale)
    filtered = set(form.filtered())
    return {
        "rows": rows,
        "total": total,
        "offset": form.offset,
        "limit": form.limit,
        "query": form.query(offset="", limit=""),
        "return_to": return_to,
        "show_user": "user" not in filtered,
        "show_project": project is None and "project" not in filtered,
        "show_tool": "tool" not in filtered,
        "show_model": "model" not in filtered,
        "show_issue": "issue" not in filtered,
    }


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


# ---------------------------------------------------------------------------
# Spend by person
# ---------------------------------------------------------------------------


def _initials(name: str) -> str:
    words = [w for w in name.replace(".", " ").replace("_", " ").split() if w]
    if len(words) >= 2:
        return (words[0][0] + words[-1][0]).upper()
    return name[:2].upper() if name else "?"


def _payment(usage: PersonUsage | None, plans: list[str], instance: bool, seat_shown: bool) -> str:
    """How a person's usage is paid: plan names (instance report), a seat, or the cost types only.

    Plan names only on the instance report (administrators); a project report
    that shows seat cost says "subscription seat"; without seat cost only the
    cost types the entries carry.
    """
    cost_types = usage.cost_types if usage is not None else set()
    parts: list[str] = []
    if "subscription" in cost_types or plans:
        if seat_shown and instance and plans:
            parts.extend(plans)
        elif seat_shown:
            parts.append(_("Subscription seat"))
        else:
            parts.append(_("Subscription"))
    labels = {"api": _("Pay-per-use API"), "local": _("Local model"), "free": _("Free")}
    parts.extend(labels[c] for c in ("api", "local", "free") if c in cost_types)
    return " + ".join(parts)


async def _people_view(
    db: AsyncSession,
    user: User,
    project: Project | None,
    form: ReportForm,
    report: SpendReport,
    scope: Scope,
    charts: dict[str, Any] | None,
    base_url: str,
) -> dict[str, Any] | None:
    """The "Spend by person" cards of the current level, from Redis when built in the last minute.

    The :data:`PEOPLE_TOP` people with the largest spend, and one summary
    card with how many more there are and their spend (exact: the sum of
    their rows), which opens the Person level of the drill-down, ordered by
    spend (that table folds past :data:`DRILL_MAX_ROWS` rows). Tools, cost
    types and plans are looked up for the people shown only.

    Cached with the same key parts as the charts (scope, reader, what the
    reader may see, every filter, the data version), so readers never share it.
    """
    if form.user_id:
        return None

    async def build() -> dict[str, Any] | None:
        if report.group_by == ("user",) and not report.period:
            by_user = report
        else:
            by_user = await _reports.report(
                db,
                user,
                project=project,
                date_from=report.date_from,
                date_to=report.date_to,
                group_by=("user",),
                currency=report.currency,
                rate_policy=report.rate_policy,
                include_seat_cost=report.include_seat_cost,
                filters=scope.filters,
                sort=form.sort,
            )
        rows = [r for r in by_user.rows if r.key.get("user") is not None]
        if not rows:
            return None
        # The largest by spend (ties by name); the rest are summed into one card.
        ranked = sorted(rows, key=lambda r: (-_spend(r), r.labels.get("user", "").lower(), r.key["user"]))
        top, rest = ranked[:PEOPLE_TOP], ranked[PEOPLE_TOP:]
        if len(rest) == 1:
            top, rest = ranked, []
        instance = project is None
        seat_shown = by_user.seat_cost_shown
        ids = [r.key["user"] for r in top]
        usage = await _reports.person_usage(
            db,
            user,
            project=project,
            date_from=report.date_from,
            date_to=report.date_to,
            filters=scope.filters,
            users=ids,
        )
        plans: dict[int, list[str]] = {}
        if seat_shown and instance:
            plans = await _plan_names(db, by_user, ids, usage)
        tool_names = await _tool_names(db)
        locale = get_locale()
        slots = _slot_map("user", charts, rows)
        largest = max((_spend(r) for r in top), default=Decimal(0))
        filtered = set(form.filtered()) | {"user"}
        cards = []
        for row in top:
            uid = row.key["user"]
            name = row.labels.get("user", "") or str(uid)
            used = usage.get(uid)
            cards.append(
                {
                    "name": name,
                    "initials": _initials(name),
                    "slot": _slot(slots, "user", uid),
                    "spend": row.total_spend.total.format(locale) if row.total_spend is not None else "–",
                    "width": _share(_spend(row), largest)["width"],
                    "tools": [tool_names.get(t, t) for t in used.tools] if used else [],
                    "payment": _payment(used, plans.get(uid, []), instance, seat_shown),
                    "url": base_url + "?" + form.level_query(next_group("user", filtered, instance), user_id=str(uid)),
                }
            )
        more = None
        if rest:
            summed = fold_rows(rest, by_user.currency, seat_shown)
            more = {
                "count": len(rest),
                "spend": summed.total_spend.total.format(locale) if summed.total_spend is not None else "–",
                "url": base_url + "?" + form.level_query("user", sort="spend"),
            }
        return {"cards": cards, "more": more}

    parts = await _cache_parts(db, user, project, report)
    parts.update(sort=form.sort, level=form.query(offset="", limit="", bucket="", detail="", **NO_SHOWS))
    prefix = f"aiu:spend-people:{report.scope}:{project.id if project is not None else 0}:{user.id}"
    return await cached_chart_payload(db, prefix, parts, _version_stmt(project), build)


async def _plan_names(
    db: AsyncSession, report: SpendReport, users: list[int], usage: dict[int, PersonUsage]
) -> dict[int, list[str]]:
    """Plan names of *users* (instance report only): plans their entries used, and idle plans whose fee counts."""
    used = {sid for u in usage.values() for sid in u.subscription_ids}
    condition: Any = AiSubscription.id.in_(used) if used else None
    if report.unallocated_shown:
        idle = (
            AiSubscription.user_id.in_(users)
            & (AiSubscription.starts_on <= report.date_to)
            & (AiSubscription.ends_on.is_(None) | (AiSubscription.ends_on >= report.date_from))
        )
        condition = idle if condition is None else (condition | idle)
    if condition is None:
        return {}
    stmt = select(AiSubscription.user_id, AiSubscription.plan_name).where(condition).order_by(AiSubscription.plan_name)
    out: dict[int, list[str]] = {}
    for uid, name in (await db.execute(stmt)).all():
        if name not in out.setdefault(uid, []):
            out[uid].append(name)
    return out


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


async def _page(
    request: Request,
    response: Response,
    db: AsyncSession,
    user: User,
    project: Project | None,
    form: ReportForm,
) -> Response:
    """Either report page: the report, its drill-down, the person cards and the charts."""
    instance = project is None
    form.for_scope(instance)
    await enforce_ai_report_rate_limit(request, response, user)
    if form.user and not form.user_id:
        # A login typed without JS: the person when the filter may name them, else nobody (id 0).
        found = await _usage.person_in_scope(db, user, project, login=form.user)
        form.user_id = str(found.id) if found is not None else "0"
    scope = await _resolve(db, user, project, form)
    report, error = await _run(db, user, project, form, scope)
    if project is not None and form.user_id and (report is None or not report.totals.entries):
        # A person is named on a project report only through entries the reader may see.
        scope.labels["user"] = form.user or form.user_id
    base_url = ADMIN_URL if instance else f"/projects/{project.key}/ai-usage/report/"  # type: ignore[union-attr]
    charts = drill = people = sessions = None
    if report is not None:
        charts = await _charts(db, user, project, form, report)
        dim = None if form.sessions or len(report.group_by) != 1 or report.period else report.group_by[0]
        folded = _fold(report)
        # Facts only for the rows the table shows: never a query per person of a long report.
        keys = [r.key.get(dim) for r in folded.rows if not r.seat_only] if dim else None
        facts = await _reports.drill_facts(
            db,
            user,
            project=project,
            date_from=report.date_from,
            date_to=report.date_to,
            filters=scope.filters,
            dim=dim,
            keys=keys,
            users=[k for k in keys or () if k is not None] if dim == "user" else (),
        )
        drill = await _drill_view(db, user, project, form, report, scope, facts, charts, base_url, folded)
        if form.sessions:
            sessions = await _sessions(db, user, project, form, report, scope, charts, request)
        people = await _people_view(db, user, project, form, report, scope, charts, base_url)
    if instance:
        offer_people = True
        people_endpoint = "/api/v1/ai-usage/report/people/"
    else:
        offer_people = await sees_everyone(db, user, project)  # type: ignore[arg-type]
        people_endpoint = f"/api/v1/projects/{project.key}/ai-usage/report/people/"  # type: ignore[union-attr]
    model_filter = {
        "endpoint": "/api/v1/ai-usage/report/models/"
        if instance
        else f"/api/v1/projects/{project.key}/ai-usage/report/models/",  # type: ignore[union-attr]
        "chip": (
            {"value": form.model, "label": form.model, "clear_url": base_url + "?" + form.query(model="")}
            if form.model
            else None
        ),
    }
    person_filter = {
        "offered": offer_people,
        "endpoint": people_endpoint,
        "chip": await person_filter_chip(
            db, user, project, form.user_id, form.user, base_url + "?" + form.query(user_id="")
        ),
    }
    context: dict[str, Any] = {
        "user": user,
        "active_page": "admin" if instance else "ai_usage",
        "project": project,
        "base_url": base_url,
        "charts": charts,
        "drill": drill,
        "people": people,
        "sessions": sessions,
        "bucket_links": _bucket_links(base_url, form),
        "show_links": _show_links(base_url, form),
        "csv_base": "/api/v1/ai-usage/report/" if instance else f"/api/v1/projects/{project.key}/ai-usage/report/",  # type: ignore[union-attr]
        "entries_url": ADMIN_ENTRIES_URL if instance else f"/projects/{project.key}/ai-usage/",  # type: ignore[union-attr]
        "level_entries_url": _entries_url(report, form) if report is not None else "",
        "person_filter": person_filter,
        "model_filter": model_filter,
        "error": error,
        **await _context(db, report, form),
    }
    if not instance:
        context["active_project"] = project
    return get_templates().TemplateResponse(
        request, "pages/ai_usage/report.html", context=context, status_code=422 if error else 200
    )


@router.get("/projects/{project_key}/ai-usage/report/", response_class=HTMLResponse)
async def project_spend_report(
    project_key: str,
    request: Request,
    response: Response,
    user: Annotated[User, Depends(require_user)],
    form: Annotated[ReportForm, Depends(_form_params)],
    db: AsyncSession = DB_SESSION,
) -> Response:
    """The project's AI spend report: rows from the entries the reader may see."""
    project = await _projects.get_by_key(db, project_key.upper())
    await _projects.require_project_access(db, project, user)
    return await _page(request, response, db, user, project, form)


@router.get(ADMIN_URL, response_class=HTMLResponse)
async def admin_spend_report(
    request: Request,
    response: Response,
    user: Annotated[User, Depends(require_admin)],
    form: Annotated[ReportForm, Depends(_form_params)],
    db: AsyncSession = DB_SESSION,
) -> Response:
    """The instance's AI spend report, unallocated seat cost included."""
    return await _page(request, response, db, user, None, form)


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
