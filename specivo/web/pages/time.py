"""Web time tracking pages: the time entries lists (a project's and every project's) and the log-time form."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import DB_SESSION
from specivo.core.exceptions import NotFoundError
from specivo.core.i18n import gettext as _
from specivo.core.utils import safe_int
from specivo.models.project import Project
from specivo.models.time_entry import TimeEntry
from specivo.models.user import User
from specivo.services.issue_service import IssueService
from specivo.services.permission_service import Permission, check_permission
from specivo.services.time_entry_service import TimeEntryService
from specivo.web.deps import ProjectReader, WebUser, get_templates, require_user

router = APIRouter(tags=["web-time"], include_in_schema=False)

_time_svc = TimeEntryService()
_issue_svc = IssueService()

#: Where the list of every project's time entries lives.
ALL_URL = "/time-entries/"


async def _time_project(db: AsyncSession, user: User, key: str) -> Project | None:
    """The project *key* when *user* may read its time entries (the project filter), else None."""
    wanted = key.strip().upper()
    if not wanted:
        return None
    return next((p for p in await _time_svc.time_projects(db, user, wanted) if p.key.upper() == wanted), None)


async def _issue_id(db: AsyncSession, user: User, key: str, project: Project | None) -> int | None:
    """The id of the issue *key* the viewer may see (in *project* when given).

    -1 when there is no such issue, so nothing matches; None for no filter.
    """
    if not key.strip():
        return None
    try:
        issue = await _issue_svc.get_by_display_key(db, key.strip(), user=user)
    except NotFoundError:
        return -1
    if project is not None and issue.project_id != project.id:
        return -1
    return issue.id


@dataclass
class _Filters:
    """The filters of a time entries list, as the query string gives them."""

    project: str
    user_id: str
    issue: str
    from_date: date | None
    to_date: date | None
    offset: int
    limit: int


def _person(u: User) -> dict[str, Any]:
    return {
        "id": u.id,
        "login": u.login,
        "display_name": u.display_name,
        "avatar_url": u.avatar_url or "",
        "avatar_color": str((u.preferences or {}).get("avatar_color") or ""),
    }


async def _render_list(
    request: Request,
    db: AsyncSession,
    user: User,
    project: Project | None,
    f: _Filters,
) -> Response:
    """The time entries list: of *project*, or of every project the viewer may read (*project* None).

    On the list of every project, ``f.project`` (a key) narrows it to one
    project. The project, user and issue filters are search fields
    (``filter_combobox``, ``issue_combobox``); a chosen project or person shows
    as a chip whose link clears it.
    """
    from specivo.web.pages.issues import person_chip

    project_filter, user_id, issue, from_date, to_date = f.project, f.user_id, f.issue, f.from_date, f.to_date
    offset, limit = f.offset, f.limit
    chosen = project if project is not None else await _time_project(db, user, project_filter)
    unknown_project = project is None and bool(project_filter.strip()) and chosen is None
    person_id = safe_int(user_id)
    issue_id = await _issue_id(db, user, issue, chosen)
    entries: list[TimeEntry] = []
    total, total_hours = 0, Decimal(0)
    if not unknown_project:
        entries, total, total_hours = await _time_svc.list_entries(
            db,
            user,
            project=chosen,
            user_id=person_id,
            issue_id=issue_id,
            from_date=from_date,
            to_date=to_date,
            offset=offset,
            limit=limit,
        )

    base_url = f"/projects/{project.key}/time-entries/" if project is not None else ALL_URL
    params = {
        "project": project_filter.strip().upper() if project is None else "",
        "user_id": user_id if person_id is not None else "",
        "issue": issue.strip(),
        "from_date": from_date.isoformat() if from_date else "",
        "to_date": to_date.isoformat() if to_date else "",
    }

    def query(**drop: str) -> str:
        return urlencode({k: v for k, v in params.items() if v and k not in drop})

    def clear_url(name: str) -> str:
        rest = query(**{name: ""})
        return base_url + (f"?{rest}" if rest else "")

    person_chip_data = None
    if person_id is not None:
        found = await _time_svc.search_people(db, user, chosen, user_id=person_id, limit=1)
        person_chip_data = person_chip(found[0]) if found else {"label": _("Unknown person")}
        person_chip_data.update(value=str(person_id), clear_url=clear_url("user_id"))
    project_chip = None
    if project is None and project_filter.strip():
        label = chosen.name if chosen is not None else project_filter.strip().upper()
        project_chip = {"label": label, "value": params["project"], "clear_url": clear_url("project")}

    if project is not None:
        can_log_time = await check_permission(user, project.id, Permission.LOG_TIME, db)
        can_manage = await check_permission(user, project.id, Permission.MANAGE_TIME_ENTRIES, db)
    else:
        can_log_time = can_manage = False

    return get_templates().TemplateResponse(
        request,
        "pages/time/list.html",
        context={
            "user": user,
            "active_page": "time" if project is not None else "time_all",
            "active_project": project,
            "project": project,
            "entries": entries,
            "total": total,
            "total_hours": total_hours,
            "can_log_time": can_log_time,
            "can_manage_time_entries": can_manage,
            "offset": offset,
            "limit": limit,
            "base_url": base_url,
            "page_query": query(),
            "filters": params,
            "person_chip": person_chip_data,
            "project_chip": project_chip,
            "people_endpoint": "/time-entries/people/" + (f"?project={project.key}" if project is not None else ""),
        },
    )


@router.get(ALL_URL, response_class=HTMLResponse)
async def all_time_entries(
    request: Request,
    user: WebUser,
    db: AsyncSession = DB_SESSION,
    project: str = Query("", max_length=50),
    user_id: str = Query(""),
    issue: str = Query("", max_length=50),
    from_date: date | None = Query(None),
    to_date: date | None = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=100),
) -> Response:
    """The time entries of every project the viewer may read them in, with a project filter."""
    filters = _Filters(project, user_id, issue, from_date, to_date, offset, limit)
    return await _render_list(request, db, user, None, filters)


@router.get("/projects/{project_key}/time-entries/", response_class=HTMLResponse)
async def time_entries_list(
    request: Request,
    user: WebUser,
    project: Annotated[Project, Depends(ProjectReader(Permission.VIEW_TIME_ENTRIES))],
    db: AsyncSession = DB_SESSION,
    user_id: str = Query(""),
    issue: str = Query("", max_length=50),
    from_date: date | None = Query(None),
    to_date: date | None = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=100),
) -> Response:
    """The time entries of a project."""
    filters = _Filters("", user_id, issue, from_date, to_date, offset, limit)
    return await _render_list(request, db, user, project, filters)


@router.get(ALL_URL + "projects/", response_model=None)
async def time_project_search(
    user: Annotated[User, Depends(require_user)],
    db: AsyncSession = DB_SESSION,
    q: str = Query("", max_length=100),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=50),
) -> JSONResponse:
    """Project filter suggestions: the projects whose time entries the viewer may read, ``[{id: key, name}]``."""
    projects = (await _time_svc.time_projects(db, user, q))[offset : offset + limit]
    return JSONResponse([{"id": p.key, "name": f"{p.name} ({p.key})"} for p in projects])


@router.get(ALL_URL + "people/", response_model=None)
async def time_people_search(
    user: Annotated[User, Depends(require_user)],
    db: AsyncSession = DB_SESSION,
    q: str = Query("", max_length=100),
    project: str = Query("", max_length=50),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=50),
) -> JSONResponse:
    """User filter suggestions: the people with time entries the viewer may read (in ``project`` when given)."""
    chosen = await _time_project(db, user, project)
    if project.strip() and chosen is None:
        return JSONResponse([])
    people = await _time_svc.search_people(db, user, chosen, q, offset=offset, limit=limit)
    return JSONResponse([_person(u) for u in people])


@router.get(ALL_URL + "issues/", response_model=None)
async def time_issue_search(
    user: Annotated[User, Depends(require_user)],
    db: AsyncSession = DB_SESSION,
    q: str = Query("", max_length=100),
    project: str = Query("", max_length=50),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=50),
) -> JSONResponse:
    """Issue filter suggestions: issues the viewer may see in projects whose time entries they may read."""
    keys = {p.key for p in await _time_svc.time_projects(db, user)}
    wanted = project.strip().upper()
    if wanted:
        keys &= {wanted}
    rows = await _issue_svc.autocomplete(db, q, user, limit=limit, project_keys=keys, offset=offset)
    return JSONResponse(rows)


@router.get("/projects/{project_key}/time-entries/new/", response_class=HTMLResponse)
async def time_entry_form(
    request: Request,
    user: WebUser,
    project: Annotated[Project, Depends(ProjectReader(Permission.LOG_TIME))],
    db: AsyncSession = DB_SESSION,
) -> Response:
    """Render the log-time form."""

    activities = await _time_svc.list_activities(db)

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/time/form.html",
        context={
            "user": user,
            "active_page": "time",
            "active_project": project,
            "project": project,
            "activities": activities,
        },
    )
