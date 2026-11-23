"""The spend report's URL state (:class:`ReportForm`) and its resolved scope."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any
from urllib.parse import urlencode

from fastapi import Query
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import NotFoundError
from specivo.core.i18n import gettext as _
from specivo.models.ai_usage_entry import AI_USAGE_COST_TYPES
from specivo.models.project import Project
from specivo.models.user import User
from specivo.services.ai_spend_planning import resolve_planning
from specivo.services.ai_spend_report_service import (
    ATTENDED_FILTERS,
    GROUP_BY,
    PERIODS,
    PRESETS,
    ReportFilters,
    SpendReport,
    default_range,
    normalize_filter_provider,
    session_context_filters,
)
from specivo.services.ai_usage_chart_service import (
    BREAKDOWN_SHOWS,
    CHOSEN_SIZES,
    TOP_N,
)
from specivo.web.pages.ai_spend_reports._common import _issues, _projects
from specivo.web.pages.ai_usage._common import NONE_VALUE, _parse_date
from specivo.web.pages.ai_usage.charts import BREAKDOWNS
from specivo.web.pages.ai_usage.rows import _tool_names

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
DRILL_ORDER: tuple[str, ...] = (
    "project",
    "version",
    "sprint",
    "user",
    "issue",
    "tool",
    "model",
    "provider",
    "cost_type",
    "branch",
    "attended",
    "profile",
)
FILTER_FIELD = {
    "project": "project",
    "version": "version",
    "sprint": "sprint",
    "user": "user_id",
    "issue": "issue",
    "tool": "tool",
    "model": "model",
    "provider": "provider",
    "cost_type": "cost_type",
    "branch": "branch",
    "attended": "attended",
    "profile": "profile",
}
#: Where a click on a row of each grouping leads: the first of these not filtered yet, else the sessions.
DRILL_NEXT: dict[str, tuple[str, ...]] = {
    "project": ("user", "issue"),
    "version": ("user", "issue"),
    "sprint": ("user", "issue"),
    "user": ("issue",),
    "issue": (),
    "tool": ("user", "issue"),
    "model": ("user", "issue"),
    "provider": ("model", "user", "issue"),
    "cost_type": ("user", "issue"),
    "branch": (),
    "attended": ("user", "issue"),
    "profile": ("user", "issue"),
}
#: Page sizes of the sessions level.
SESSION_LIMITS = (10, 25, 50)


def _planning_value(value: str) -> str:
    """A release or sprint filter as the URL may hold it: an id or ``-``; anything else is no filter."""
    return value if value == NONE_VALUE or (value.isdecimal() and len(value) < 10) else ""


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
    ``version`` and ``sprint`` (an id, ``-`` for none),
    ``issue`` (a key, ``-`` for none), ``branch`` (``-`` for none),
    ``provider``, the grouping ``group`` (``sessions``: the entries), the
    sessions page (``offset``, ``limit``) and ``detail`` (every amount column).
    ``attended`` is ``attended`` or ``unattended``; ``profile`` an agent
    profile, ``-`` for none.
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
    #: The release filter: a version id, or ``-`` for entries without a release.
    version: str = ""
    #: The sprint filter: a sprint id, or ``-`` for entries without a sprint.
    sprint: str = ""
    attended: str = ""
    profile: str = ""
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
            version=_planning_value(text("version")),
            sprint=_planning_value(text("sprint")),
            attended=text("attended") if text("attended") in ATTENDED_FILTERS else "",
            profile=text("profile")[:100],
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
            "version": self.version,
            "sprint": self.sprint,
            "attended": self.attended,
            "profile": self.profile,
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
            "version": self.version,
            "sprint": self.sprint,
            "attended": self.attended,
            "profile": self.profile,
            "format": "csv",
        }
        return urlencode({k: v for k, v in values.items() if v != ""})


def _form_params(  # noqa: PLR0913
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
    version: str = Query(""),
    sprint: str = Query(""),
    attended: str = Query(""),
    profile: str = Query(""),
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
        version=version,
        sprint=sprint,
        attended=attended,
        profile=profile,
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
    _name_session_context(form, scope)
    planning = await _resolve_planning(db, user, project or scope.selected, form, scope)
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
        **planning,
        **session_context_filters(form.attended, form.profile),
    )
    return scope


def _name_session_context(form: ReportForm, scope: Scope) -> None:
    """The breadcrumb labels of the run (attended or not) and agent profile filters."""
    if form.attended:
        scope.labels["attended"] = attended_text(ATTENDED_FILTERS[form.attended])
    if form.profile:
        scope.labels["profile"] = _("No profile") if form.profile == NONE_VALUE else form.profile


async def _resolve_planning(
    db: AsyncSession, user: User, project: Project | None, form: ReportForm, scope: Scope
) -> dict[str, Any]:
    """The release and sprint filters (``ReportFilters`` fields) of *form*, and their breadcrumb labels.

    An id the reader may not see matches nothing and is named by its number only.
    """
    fields: dict[str, Any] = {}
    for dim, none_label in (("version", _("No release")), ("sprint", _("No sprint"))):
        value = getattr(form, dim)
        if value == NONE_VALUE:
            fields[f"without_{dim}"] = True
            scope.labels[dim] = none_label
        elif value:
            found = await resolve_planning(db, user, dim, value, project)
            fields[f"{dim}_id"] = found.id
            scope.labels[dim] = found.label if found.id != -1 else value
    return fields


def attended_text(unattended: Any) -> str:
    """The name of an ``attended`` row or filter: by the entries' ``unattended`` flag."""
    return _("Unattended") if unattended else _("Attended")


def _cost_type_text(value: str) -> str:
    return {
        "subscription": _("Subscription"),
        "api": _("API (pay per use)"),
        "local": _("Local model"),
        "free": _("Free"),
    }.get(value, _("Unknown"))
