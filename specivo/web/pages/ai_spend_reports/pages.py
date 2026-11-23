"""The spend report pages: the project report, the admin report and its entry list."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.ai_report_limits import enforce_ai_report_rate_limit
from specivo.core.database import DB_SESSION
from specivo.core.exceptions import NotFoundError
from specivo.models.project import Project
from specivo.models.user import User
from specivo.services.ai_spend_report_service import SpendReport
from specivo.web.deps import get_templates, require_user
from specivo.web.pages.admin import require_admin
from specivo.web.pages.ai_spend_reports._common import ADMIN_ENTRIES_URL, ADMIN_URL, _projects, _reports, _usage
from specivo.web.pages.ai_spend_reports.drill import _drill_view, _fold, _sessions
from specivo.web.pages.ai_spend_reports.form import ReportForm, Scope, _form_params, _resolve
from specivo.web.pages.ai_spend_reports.people import _people_view
from specivo.web.pages.ai_spend_reports.view import _bucket_links, _charts, _context, _entries_url, _run, _show_links
from specivo.web.pages.ai_usage._common import NONE_VALUE, _parse_date
from specivo.web.pages.ai_usage.filters import _filters, _issue_filter, person_filter_chip, sees_everyone
from specivo.web.pages.ai_usage.rows import _rows

router = APIRouter(tags=["web-ai-usage"], include_in_schema=False)


@dataclass(frozen=True)
class ReportUrls:
    """Where a report page and its links point: a project's report, or the instance's (admin)."""

    #: The page itself; the drill-down, bucket and chip links build on it.
    base: str
    #: The CSV export (the REST report with the same query).
    csv: str
    #: The entry list a cell drills through to.
    entries: str
    #: The person search behind the person filter.
    people: str
    #: The model search behind the model filter.
    models: str

    @classmethod
    def of(cls, project: Project | None) -> ReportUrls:
        if project is None:
            return cls(
                base=ADMIN_URL,
                csv="/api/v1/ai-usage/report/",
                entries=ADMIN_ENTRIES_URL,
                people="/api/v1/ai-usage/report/people/",
                models="/api/v1/ai-usage/report/models/",
            )
        api = f"/api/v1/projects/{project.key}/ai-usage/report/"
        return cls(
            base=f"/projects/{project.key}/ai-usage/report/",
            csv=api,
            entries=f"/projects/{project.key}/ai-usage/",
            people=f"{api}people/",
            models=f"{api}models/",
        )


async def _scope_for(
    request: Request,
    response: Response,
    db: AsyncSession,
    user: User,
    project: Project | None,
    form: ReportForm,
) -> Scope:
    """Settle the form for the page's scope, apply the rate limit and resolve the drill filters."""
    form.for_scope(project is None)
    await enforce_ai_report_rate_limit(request, response, user)
    if form.user and not form.user_id:
        # A login typed without JS: the person when the filter may name them, else nobody (id 0).
        found = await _usage.person_in_scope(db, user, project, login=form.user)
        form.user_id = str(found.id) if found is not None else "0"
    return await _resolve(db, user, project, form)


async def _sections(
    request: Request,
    db: AsyncSession,
    user: User,
    project: Project | None,
    form: ReportForm,
    report: SpendReport,
    scope: Scope,
    base_url: str,
) -> dict[str, Any]:
    """The charts, the drill-down table, the sessions list and the person cards of a report."""
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
    sessions = await _sessions(db, user, project, form, report, scope, charts, request) if form.sessions else None
    people = await _people_view(db, user, project, form, report, scope, charts, base_url)
    return {"charts": charts, "drill": drill, "people": people, "sessions": sessions}


async def _filter_chips(
    db: AsyncSession, user: User, project: Project | None, form: ReportForm, urls: ReportUrls
) -> dict[str, Any]:
    """The person and model filters: their search endpoints and the chip of a chosen value."""
    offer_people = True if project is None else await sees_everyone(db, user, project)
    model_filter = {
        "endpoint": urls.models,
        "chip": (
            {"value": form.model, "label": form.model, "clear_url": urls.base + "?" + form.query(model="")}
            if form.model
            else None
        ),
    }
    person_filter = {
        "offered": offer_people,
        "endpoint": urls.people,
        "chip": await person_filter_chip(
            db, user, project, form.user_id, form.user, urls.base + "?" + form.query(user_id="")
        ),
    }
    return {"person_filter": person_filter, "model_filter": model_filter}


async def _page(
    request: Request,
    response: Response,
    db: AsyncSession,
    user: User,
    project: Project | None,
    form: ReportForm,
) -> Response:
    """Either report page: the report, its drill-down, the person cards and the charts."""
    scope = await _scope_for(request, response, db, user, project, form)
    report, error = await _run(db, user, project, form, scope)
    if project is not None and form.user_id and (report is None or not report.totals.entries):
        # A person is named on a project report only through entries the reader may see.
        scope.labels["user"] = form.user or form.user_id
    urls = ReportUrls.of(project)
    sections: dict[str, Any] = dict.fromkeys(("charts", "drill", "people", "sessions"))
    if report is not None:
        sections = await _sections(request, db, user, project, form, report, scope, urls.base)
    context: dict[str, Any] = {
        "user": user,
        "active_page": "admin" if project is None else "ai_usage",
        "project": project,
        "base_url": urls.base,
        **sections,
        "bucket_links": _bucket_links(urls.base, form),
        "show_links": _show_links(urls.base, form),
        "csv_base": urls.csv,
        "entries_url": urls.entries,
        "level_entries_url": _entries_url(report, form) if report is not None else "",
        **await _filter_chips(db, user, project, form, urls),
        "error": error,
        **await _context(db, report, form),
    }
    if project is not None:
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
async def admin_spend_entries(  # noqa: PLR0913
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
    version: str = Query(""),
    sprint: str = Query(""),
    attended: str = Query(""),
    profile: str = Query("", max_length=100),
    payer: str = Query(""),
    billing_account: str = Query(""),
    route: str = Query("", max_length=100),
    scope: str = Query(""),
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
        version=version,
        sprint=sprint,
        attended=attended,
        profile=profile,
        payer=payer,
        billing_account=billing_account,
        route=route,
        scope=scope,
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
    await filters.name_planning(db, user, selected)
    await filters.name_billing(db, user)
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
