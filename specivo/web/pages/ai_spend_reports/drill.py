"""The spend report drill-down: folded rows, side cards, path, tabs and sessions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.i18n import get_locale, ngettext
from specivo.core.i18n import gettext as _
from specivo.models.ai_usage_entry import AiUsageEntry
from specivo.models.issue import Issue
from specivo.models.project import Project
from specivo.models.user import User
from specivo.services.ai_spend_report_service import (
    GROUP_BY,
    HIDDEN_ISSUE,
    DrillFacts,
    ReportRow,
    SpendReport,
    fold_rows,
    period_end,
)
from specivo.services.ai_usage_chart_service import (
    NEUTRAL_SLOT,
    NO_PROJECT,
    color_slots,
)
from specivo.web.pages.ai_spend_reports._common import _reports, _usage
from specivo.web.pages.ai_spend_reports.form import FILTER_FIELD, SESSIONS, ReportForm, Scope, next_group
from specivo.web.pages.ai_spend_reports.view import _cell_text, _row_view, _short_int
from specivo.web.pages.ai_usage._common import NONE_VALUE
from specivo.web.pages.ai_usage.rows import _rows, _tool_names

#: Rows of a drill-down table; the rest are summed into one "other" row (the CSV has them all).
DRILL_MAX_ROWS = 30
#: Groups a drill-down table with a period ("Per week") shows in each period, the largest by
#: spend over the whole range; the rest are summed into one "other" row per period.
DRILL_PERIOD_GROUPS = 10
#: Bars of the "Spend by ..." card.
SIDE_BARS = 8


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
