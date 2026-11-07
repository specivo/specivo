"""The AI usage tab of an issue: what the feature cost, at a glance, with its sessions a page at a time.

- ``/partials/issues/{key}/ai-usage/``: the tab, loaded by htmx when it opens
  on the issue page, and swapped in again by the tab's own links;
- ``/issue/{key}/ai-usage/``: the same view as a page of its own, so every
  link of the tab (sub-issue switch, pages, page sizes) also works without
  JavaScript and can be shared.

What it shows (:func:`_summary`): the headline numbers (spend, API-equivalent
value, tokens, entries, people, first and last day), spend by person, by tool
and by model (the largest few and one "other"), spend over time when the
usage spans :data:`CHART_MIN_DAYS` days or more, and the sessions (the
entries, newest first, :data:`PAGE_SIZES` per page). An issue with sub-issues
counts their usage too unless the reader switches to "this issue only"
(``sub=0``); without sub-issues there is no switch.

Every number comes from :class:`AiSpendReportService` over the issue's
entries the reader may read, with the project report's rules: seat cost is
part of the spend only for a reader who sees every entry of the project in
the range (``sees_every_entry``), otherwise it is left out and the tab says
so. The totals are SQL sums over all entries, never over the page shown. The
summary is cached per reader like the report charts
(:func:`~specivo.web.pages.ai_usage.cached_chart_payload`: keyed by the issue,
the switch, the reader and what they may see, and the project's data version);
each request spends one AI report from the reader's budget
(:func:`~specivo.core.ai_report_limits.enforce_ai_report_rate_limit`).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.ai_report_limits import enforce_ai_report_rate_limit
from specivo.core.database import DB_SESSION
from specivo.core.exceptions import NotFoundError
from specivo.core.i18n import format_date, get_locale
from specivo.models.ai_usage_entry import AiUsageEntry
from specivo.models.issue import Issue
from specivo.models.project import Project
from specivo.models.user import User
from specivo.services.ai_spend_report_service import (
    MAX_RANGE_DAYS,
    AiSpendReportService,
    ReportFilters,
    ReportRow,
    SpendReport,
    fold_rows,
)
from specivo.services.ai_usage_chart_service import NEUTRAL_SLOT, TOP_N, AiUsageChartService, color_slots
from specivo.services.ai_usage_service import AiUsageService
from specivo.services.issue_service import IssueService
from specivo.services.permission_service import Permission, ai_usage_visibility, check_permission
from specivo.web.deps import get_templates, require_user
from specivo.web.pages.ai_spend_reports import _initials, _short_int, _version_stmt
from specivo.web.pages.ai_usage import (
    _charts_view,
    _fmt_int,
    _money_view,
    _project_for,
    _rows,
    _tool_names,
    cached_chart_payload,
)

router = APIRouter(tags=["web-ai-usage"], include_in_schema=False)

_reports = AiSpendReportService()
_usage = AiUsageService()
_issues = IssueService()

#: Page sizes of the sessions list; the first is the default.
PAGE_SIZES = (10, 25, 50)
#: Bars of the "by tool" and "by model" lists; the rest are one "other" bar.
BARS = TOP_N
#: Person cards shown; the rest are counted.
PEOPLE = TOP_N
#: The spend-over-time chart is drawn when the usage spans at least this many days.
CHART_MIN_DAYS = 4
#: Where the user guide explains how usage is attributed to an issue.
ATTRIBUTION_GUIDE_URL = "https://specivo.io/docs/specivo/ai-agents/reporting-ai-usage/#which-issue-the-usage-counts-for"


@dataclass
class TabState:
    """The tab's state, all of it in the URL: the sub-issue switch and the sessions page."""

    issue_key: str
    #: Whether the issue has sub-issues (only then is there a switch).
    has_children: bool
    #: Whether the numbers include the sub-issues' usage.
    sub: bool
    offset: int = 0
    limit: int = PAGE_SIZES[0]

    @classmethod
    def read(cls, issue_key: str, has_children: bool, sub: str, offset: str, limit: str) -> TabState:
        """Clean the query: unknown values fall back to the defaults (sub-issues included when there are any)."""
        size = int(limit) if limit.isdigit() and int(limit) in PAGE_SIZES else PAGE_SIZES[0]
        start = int(offset) if offset.isdigit() and int(offset) < 10_000_000 else 0
        return cls(
            issue_key=issue_key,
            has_children=has_children,
            sub=has_children and sub != "0",
            offset=start - start % size,
            limit=size,
        )

    def query(self, **override: Any) -> str:
        """The query of this state with *override*, defaults left out."""
        values: dict[str, Any] = {"sub": self.sub, "offset": self.offset, "limit": self.limit, **override}
        params = {
            "sub": "0" if self.has_children and not values["sub"] else "",
            "offset": str(values["offset"]) if values["offset"] else "",
            "limit": str(values["limit"]) if values["limit"] != PAGE_SIZES[0] else "",
        }
        return urlencode({k: v for k, v in params.items() if v})

    def link(self, **override: Any) -> dict[str, str]:
        """The page URL (``href``, works without JavaScript) and the partial URL (``hx-get``) of a state."""
        query = self.query(**override)
        suffix = f"?{query}" if query else ""
        return {
            "href": f"/issue/{self.issue_key}/ai-usage/{suffix}",
            "hx": f"/partials/issues/{self.issue_key}/ai-usage/{suffix}",
        }


def _filters(issue: Issue, sub: bool) -> ReportFilters:
    return ReportFilters(issue_tree_id=issue.id) if sub else ReportFilters(issue_id=issue.id)


def _spend(row: ReportRow) -> Decimal:
    return row.total_spend.total.amount if row.total_spend is not None else Decimal(0)


def _api(row: ReportRow) -> Decimal:
    return row.api_equivalent.total.amount if row.api_equivalent is not None else Decimal(0)


def _width(part: Decimal, largest: Decimal) -> float:
    """A bar width (0-100) relative to the largest bar."""
    if largest <= 0 or part <= 0:
        return 0.0
    return round(min(float(part / largest * 100), 100.0), 1)


def _grouped(report: SpendReport, dim: str) -> list[tuple[Any, str, ReportRow]]:
    """The report's rows summed per value of *dim*: ``(key, label, row)``, largest spend (then value) first."""
    parts: dict[Any, list[ReportRow]] = defaultdict(list)
    labels: dict[Any, str] = {}
    for row in report.rows:
        key = row.key.get(dim)
        parts[key].append(row)
        labels.setdefault(key, row.labels.get(dim, "") or "")
    out = [(key, labels[key], fold_rows(rows, report.currency, report.seat_cost_shown)) for key, rows in parts.items()]
    out.sort(key=lambda g: (-_spend(g[2]), -_api(g[2]), g[1].lower()))
    return out


def _stack_slots(chart: dict[str, Any] | None, dim: str) -> dict[str, int]:
    """The colour slots of the chart's stack for *dim* (``{key: slot}``), empty without a chart."""
    stack = ((chart or {}).get("stacks") or {}).get(dim) or {}
    return {str(k): int(c) for k, c in zip(stack.get("keys") or [], stack.get("colors") or [], strict=False)}


def _bars(
    groups: list[tuple[Any, str, ReportRow]],
    measure: str,
    slots: dict[str, int],
    report: SpendReport,
    locale: str,
    names: dict[str, str] | None = None,
) -> dict[str, Any]:
    """A compact bar list: the largest :data:`BARS` groups and one "other" bar summing the rest."""
    value = _spend if measure == "spend" else _api

    def text(row: ReportRow) -> str:
        summary = row.total_spend if measure == "spend" else row.api_equivalent
        return summary.total.format(locale) if summary is not None else "–"

    shown, rest = (groups, []) if len(groups) <= BARS + 1 else (groups[:BARS], groups[BARS:])
    fallback = color_slots(str(k) for k, _label, _row in shown)
    largest = max((value(r) for _k, _l, r in shown), default=Decimal(0))
    bars = [
        {
            "label": (names or {}).get(str(key), label) or label or "–",
            "slot": slots.get(str(key), NEUTRAL_SLOT) if slots else fallback[str(key)],
            "amount": text(row),
            "entries": row.entries,
            "width": _width(value(row), largest),
        }
        for key, label, row in shown
    ]
    other = None
    if rest:
        folded = fold_rows([r for _k, _l, r in rest], report.currency, report.seat_cost_shown)
        other = {"groups": len(rest), "amount": text(folded), "entries": folded.entries}
    return {"bars": bars, "other": other}


async def _span(db: AsyncSession, user: User, project: Project, filters: ReportFilters) -> tuple[date, date] | None:
    """The first and last day of the issue's counted entries the reader may read (one min/max)."""
    conditions = await _usage._list_conditions(db, user, project=project, issue_id=filters.issue_id, excluded=False)
    stmt = select(func.min(AiUsageEntry.usage_date), func.max(AiUsageEntry.usage_date)).where(
        *conditions, *filters.conditions()
    )
    first, last = (await db.execute(stmt)).one()
    return (first, last) if first is not None else None


async def _summary(
    db: AsyncSession, user: User, project: Project, issue: Issue, state: TabState
) -> dict[str, Any] | None:
    """Headline numbers, people, tools, models and the time chart of the issue (None without usage).

    One spend report grouped by person, tool and model (folded per dimension
    here), the report's drill facts, and the time chart; bounded whatever the
    number of entries. Cached per reader (see the module docstring).
    """
    filters = _filters(issue, state.sub)

    async def build() -> dict[str, Any] | None:
        span = await _span(db, user, project, filters)
        if span is None:
            return None
        first, last = span
        clipped = (last - first).days + 1 > MAX_RANGE_DAYS
        if clipped:
            first = last - timedelta(days=MAX_RANGE_DAYS - 1)
        report = await _reports.report(
            db,
            user,
            project=project,
            date_from=first,
            date_to=last,
            group_by=("user", "tool", "model"),
            filters=filters,
            sort="spend",
        )
        if not report.totals.entries:
            return None
        facts = await _reports.drill_facts(
            db, user, project=project, date_from=first, date_to=last, filters=filters, dim=None
        )
        names = await _tool_names(db)
        locale = get_locale()
        chart = None
        if (last - first).days + 1 >= CHART_MIN_DAYS:
            charts = await AiUsageChartService().report_charts(
                db,
                user,
                project=project,
                date_from=first,
                date_to=last,
                currency=report.currency,
                rate_policy=report.rate_policy,
                filters=filters,
                tool_names=names,
            )
            view = _charts_view(charts)
            if view and not view.get("too_long"):
                chart = {"time": view["time"], "bucket": view["bucket"], "unpriced": view["unpriced"]}
        totals = report.totals
        total_spend = _spend(totals)
        # Bars measure spend; when nothing in view has any (a seat the reader may not see), the value.
        measure = "spend" if total_spend > 0 else "api"
        time = chart["time"] if chart else None

        people_groups = _grouped(report, "user")
        user_slots = _stack_slots(time, "user") or {
            str(k): s for k, s in color_slots(str(k) for k, _l, _r in people_groups).items()
        }
        value = _spend if measure == "spend" else _api
        largest = max((value(r) for _k, _l, r in people_groups), default=Decimal(0))
        people = []
        for uid, name, row in people_groups[:PEOPLE]:
            usage = facts.usage.get(uid)
            people.append(
                {
                    "name": name or str(uid),
                    "initials": _initials(name or str(uid)),
                    "slot": user_slots.get(str(uid), NEUTRAL_SLOT),
                    "spend": row.total_spend.total.format(locale) if row.total_spend is not None else "–",
                    "api": row.api_equivalent.total.format(locale) if row.api_equivalent is not None else "–",
                    "entries": row.entries,
                    "width": _width(value(row), largest),
                    "tools": [names.get(t, t) for t in usage.tools] if usage else [],
                }
            )
        return {
            "currency": report.currency,
            "first": first.isoformat(),
            "last": last.isoformat(),
            "first_text": format_date(span[0] if not clipped else first, "yMMMd"),
            "last_text": format_date(last, "yMMMd"),
            "clipped": clipped,
            "days": facts.days,
            "entries": totals.entries,
            "unpriced": totals.unpriced_entries,
            "unpriced_models": [{"model": m, "count": n} for m, n in report.unpriced_models],
            "people_count": facts.people,
            "tokens": _fmt_int(totals.total_tokens, locale),
            "tokens_short": _short_int(totals.total_tokens, locale),
            "spend": _money_view(totals.total_spend, locale) if totals.total_spend is not None else None,
            "api": _money_view(totals.api_equivalent, locale) if totals.api_equivalent is not None else None,
            "seat_shown": report.seat_cost_shown,
            "seat_hidden": report.seat_cost_hidden,
            "measure": measure,
            "people": people,
            "people_more": max(0, len(people_groups) - PEOPLE),
            "user_slots": user_slots,
            "tools": _bars(_grouped(report, "tool"), measure, _stack_slots(time, "tool"), report, locale, names),
            "models": _bars(_grouped(report, "model"), measure, _stack_slots(time, "model"), report, locale),
            "chart": chart,
        }

    parts = {
        "issue": issue.id,
        "sub": state.sub,
        "viewer": user.id,
        "admin": bool(user.is_admin),
        "visibility": await ai_usage_visibility(db, user, project),
        "filters": filters.cache_parts(),
    }
    prefix = f"aiu:issue-tab:{project.id}:{issue.id}:{user.id}"
    return await cached_chart_payload(db, prefix, parts, _version_stmt(project), build)


async def _sessions(
    db: AsyncSession,
    user: User,
    project: Project,
    issue: Issue,
    state: TabState,
    summary: dict[str, Any],
    return_to: str,
) -> dict[str, Any]:
    """One page of the counted entries, newest first, each with its spend (seat share where shown)."""
    issue_args: dict[str, Any] = {"issue_tree_id": issue.id} if state.sub else {"issue_id": issue.id}
    first, last = date.fromisoformat(summary["first"]), date.fromisoformat(summary["last"])
    entries, total = await _usage.list_entries(
        db,
        user,
        project=project,
        date_from=first if summary["clipped"] else None,
        excluded=False,
        offset=state.offset,
        limit=state.limit,
        **issue_args,
    )
    if not entries and state.offset and total:
        # A page past the end (entries were deleted since): show the last page instead.
        state.offset = (total - 1) // state.limit * state.limit
        entries, total = await _usage.list_entries(
            db,
            user,
            project=project,
            date_from=first if summary["clipped"] else None,
            excluded=False,
            offset=state.offset,
            limit=state.limit,
            **issue_args,
        )
    refs = await _usage.references(db, user, entries)
    rows = await _rows(db, user, entries, refs)
    spend = await _reports.entry_spend(
        db,
        user,
        project=project,
        date_from=min((e.usage_date for e in entries), default=first),
        date_to=max((e.usage_date for e in entries), default=last),
        entry_ids=[e.id for e in entries],
        currency=summary["currency"],
    )
    locale = get_locale()
    slots: dict[str, int] = summary.get("user_slots") or {}
    for entry, row in zip(entries, rows, strict=True):
        priced = spend.get(entry.id)
        row["spend"] = priced.total_spend.total.format(locale) if priced and priced.total_spend else "–"
        row["slot"] = slots.get(str(entry.user_id), NEUTRAL_SLOT)
        row["tokens_short"] = _short_int(sum(entry.all_tokens().values()), locale)
    pages = []
    last_page = (total - 1) // state.limit if total else 0
    current = state.offset // state.limit
    for page in range(last_page + 1):
        if page in (0, last_page) or abs(page - current) <= 2:
            pages.append({"number": page + 1, "current": page == current, **state.link(offset=page * state.limit)})
        elif not pages[-1].get("gap"):
            pages.append({"gap": True})
    return {
        "rows": rows,
        "total": total,
        "first": state.offset + 1 if total else 0,
        "last": min(state.offset + state.limit, total),
        "pages": pages if last_page else [],
        "prev": state.link(offset=state.offset - state.limit) if current > 0 else None,
        "next": state.link(offset=state.offset + state.limit) if current < last_page else None,
        "sizes": [{"size": s, "current": s == state.limit, **state.link(offset=0, limit=s)} for s in PAGE_SIZES],
        "return_to": return_to,
        "show_issue": state.sub,
    }


async def _context(
    request: Request,
    response: Response,
    db: AsyncSession,
    user: User,
    issue_ref: str,
    sub: str,
    offset: str,
    limit: str,
) -> dict[str, Any]:
    """Everything the tab shows; raises ``NotFoundError`` for an issue the reader cannot see."""
    issue = await _issues.get_by_display_key(db, issue_ref, user=user)
    project = await _project_for(db, user, issue.project_key)
    await enforce_ai_report_rate_limit(request, response, user)
    has_children = bool(await db.scalar(select(exists().where(Issue.parent_id == issue.id))))
    state = TabState.read(issue.display_key, has_children, sub, offset, limit)
    summary = await _summary(db, user, project, issue, state)
    return_to = f"/issue/{issue.display_key}/#ai-usage"
    sessions = await _sessions(db, user, project, issue, state, summary, return_to) if summary else None
    excluded_args: dict[str, Any] = {"issue_tree_id": issue.id} if state.sub else {"issue_id": issue.id}
    _none, excluded = await _usage.list_entries(db, user, project=project, excluded=True, limit=0, **excluded_args)
    base = f"/projects/{project.key}/ai-usage/"
    report_url = ""
    if summary:
        report_url = f"{base}report/?" + urlencode(
            {
                "preset": "custom",
                "from_date": summary["first"],
                "to_date": summary["last"],
                "issue": issue.display_key,
                "group": "user",
            }
        )
    return {
        "user": user,
        "issue": issue,
        "project": project,
        "state": state,
        "summary": summary,
        "sessions": sessions,
        "excluded": excluded,
        "can_log": await check_permission(user, project.id, Permission.LOG_AI_USAGE, db),
        "log_url": f"{base}new/?" + urlencode({"issue": issue.display_key, "next": return_to}),
        "list_url": f"{base}?" + urlencode({"issue": issue.display_key}),
        "excluded_url": f"{base}?" + urlencode({"issue": issue.display_key, "status": "excluded"}),
        "report_url": report_url,
        "sub_links": {"on": state.link(sub=True, offset=0), "off": state.link(sub=False, offset=0)},
        "guide_url": ATTRIBUTION_GUIDE_URL,
    }


@router.get("/partials/issues/{issue_ref}/ai-usage/", response_class=HTMLResponse)
async def issue_ai_usage_partial(
    issue_ref: str,
    request: Request,
    response: Response,
    user: Annotated[User, Depends(require_user)],
    db: AsyncSession = DB_SESSION,
    sub: str = Query(""),
    offset: str = Query(""),
    limit: str = Query(""),
) -> Response:
    """The issue's AI usage tab (loaded when opened, and by the tab's own links)."""
    try:
        context = await _context(request, response, db, user, issue_ref, sub, offset, limit)
    except NotFoundError:
        return HTMLResponse("", status_code=404)
    return get_templates().TemplateResponse(request, "pages/ai_usage/_issue_tab.html", context=context)


@router.get("/issue/{issue_ref}/ai-usage/", response_class=HTMLResponse)
async def issue_ai_usage_page(
    issue_ref: str,
    request: Request,
    response: Response,
    user: Annotated[User, Depends(require_user)],
    db: AsyncSession = DB_SESSION,
    sub: str = Query(""),
    offset: str = Query(""),
    limit: str = Query(""),
) -> Response:
    """The issue's AI usage as a page of its own: the tab's links without JavaScript, and a shareable view."""
    context = await _context(request, response, db, user, issue_ref, sub, offset, limit)
    context["active_project"] = context["project"]
    return get_templates().TemplateResponse(request, "pages/ai_usage/issue.html", context=context)
