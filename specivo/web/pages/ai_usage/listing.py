"""The AI usage lists: a project's "AI usage" tab and "My AI usage"."""

from __future__ import annotations

from dataclasses import replace
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.ai_report_limits import enforce_ai_report_rate_limit
from specivo.core.database import DB_SESSION
from specivo.core.exceptions import AppError, NotFoundError, PermissionDeniedError
from specivo.models.project import Project
from specivo.models.user import User
from specivo.services.ai_usage_chart_service import (
    CHOSEN_SIZES,
)
from specivo.services.currency_service import CurrencyService
from specivo.services.permission_service import Permission, check_permission
from specivo.web.deps import get_templates, require_user
from specivo.web.pages.ai_usage._common import (
    _LIST_ERRORS,
    _NOTICES,
    MY_URL,
    NONE_VALUE,
    _FormError,
    _issues,
    _next_or,
    _parse_date,
    _parse_int,
    _project_for,
    _redirect,
    _service,
    _target,
)
from specivo.web.pages.ai_usage.charts import _cached_charts_view, _project_charts_view
from specivo.web.pages.ai_usage.filters import (
    _bucket_links,
    _filter_choices,
    _filters,
    _issue_filter,
    person_filter_chip,
    sees_everyone,
)
from specivo.web.pages.ai_usage.rows import _rows, _totals

router = APIRouter(tags=["web-ai-usage"], include_in_schema=False)


@router.get("/projects/{project_key}/ai-usage/", response_class=HTMLResponse)
async def project_ai_usage(  # noqa: PLR0913
    project_key: str,
    request: Request,
    response: Response,
    user: Annotated[User, Depends(require_user)],
    db: AsyncSession = DB_SESSION,
    user_id: str = Query(""),
    person: str = Query("", alias="user", max_length=255),
    tool: str = Query(""),
    model: str = Query(""),
    cost_type: str = Query(""),
    issue: str = Query(""),
    from_date: str = Query(""),
    to_date: str = Query(""),
    status: str = Query(""),
    provider: str = Query(""),
    branch: str = Query(""),
    version: str = Query(""),
    sprint: str = Query(""),
    bucket: str = Query(""),
    offset: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=100),
    notice: str = Query(""),
    error: str = Query(""),
) -> Response:
    """The project's AI usage the reader may see, with filters, totals in its reporting currency, and charts.

    ``provider``, ``branch``, ``version`` and ``sprint`` (``-``: none) and
    ``issue=-`` (no issue) come from the spend report's drill-through. The charts group the project's
    usage, so the page spends the reader's AI report budget
    (specivo.core.ai_report_limits); the bucket asked for is only a wish.
    ``user`` is a login typed into the person filter without JS: the person
    when the filter may name them, else nobody.
    """
    project = await _project_for(db, user, project_key)
    await enforce_ai_report_rate_limit(request, response, user)
    chosen = bucket if bucket in CHOSEN_SIZES else ""
    if person.strip() and not user_id.strip().isdigit():
        found = await _service.person_in_scope(db, user, project, login=person)
        user_id = str(found.id) if found is not None else "0"
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
        version=version,
        sprint=sprint,
    )
    await filters.name_planning(db, user, project)
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
    bucket_query = urlencode({"bucket": chosen}) if chosen else ""
    clear = "&".join(q for q in (replace(filters, user_id="").query(), bucket_query) if q)
    person_filter = {
        "offered": await sees_everyone(db, user, project),
        "endpoint": f"/api/v1/projects/{project.key}/ai-usage/report/people/",
        "chip": await person_filter_chip(
            db, user, project, filters.user_id, person.strip(), base_url + (f"?{clear}" if clear else "")
        ),
    }
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
            "person_filter": person_filter,
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
async def my_ai_usage(  # noqa: PLR0913
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
    entry_ids: list[str] = Form([]),
    target_project: str = Form(""),
    target_issue: str = Form(""),
    next: str = Form(""),
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


@router.get(MY_URL + "issues/", response_model=None)
async def my_ai_usage_issue_search(
    user: Annotated[User, Depends(require_user)],
    db: AsyncSession = DB_SESSION,
    q: str = Query("", max_length=100),
    project: str = Query("", max_length=50),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=50),
) -> JSONResponse:
    """Issue suggestions for the Move form: issues in projects where the reader may log AI usage.

    ``project`` narrows them to one of those projects (the form's project
    field); an unknown or not loggable key matches nothing. An empty ``q``
    lists the most recently updated issues.
    """
    keys = {p.key for p in await _service.loggable_projects(db, user)}
    wanted = project.strip().upper()
    if wanted:
        keys = keys & {wanted}
    rows = await _issues.autocomplete(db, q, user, limit=limit, project_keys=keys, offset=offset)
    return JSONResponse(rows)
