"""Web time tracking pages: list, log-time form."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, cast

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import get_db
from specivo.core.exceptions import NotFoundError
from specivo.services.project_service import ProjectService
from specivo.services.time_entry_service import TimeEntryService
from specivo.web.deps import get_current_user_optional, get_templates

if TYPE_CHECKING:
    from specivo.models.user import User

router = APIRouter(tags=["web-time"], include_in_schema=False)

_project_svc = ProjectService()
_time_svc = TimeEntryService()


@router.get("/projects/{project_key}/time-entries", response_class=HTMLResponse)
async def time_entries_list(
    project_key: str,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
    user_id: int | None = Query(None),
    from_date: date | None = Query(None),
    to_date: date | None = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=100),
) -> Response:
    """Render the time entries list page for a project."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login", status_code=302)
    user = cast("User", user_obj)

    try:
        project = await _project_svc.get_by_key(db, project_key)
    except NotFoundError:
        return JSONResponse({"detail": "Project not found"}, status_code=404)

    entries, total = await _time_svc.list_for_project(
        db,
        project.id,
        user_id=user_id,
        from_date=from_date,
        to_date=to_date,
        offset=offset,
        limit=limit,
    )

    # Calculate total hours
    total_hours = sum(e.hours for e in entries)

    # Get activities for filter dropdown
    activities = await _time_svc.list_activities(db)

    # Get project members for user dropdown
    members = await _project_svc.list_members(db, project)

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/time/list.html",
        context={
            "user": user,
            "active_page": "time",
            "active_project": project,
            "project": project,
            "entries": entries,
            "total": total,
            "total_hours": total_hours,
            "activities": activities,
            "members": members,
            "offset": offset,
            "limit": limit,
            "filters": {
                "user_id": user_id,
                "from_date": from_date,
                "to_date": to_date,
            },
        },
    )


@router.get("/projects/{project_key}/time-entries/new", response_class=HTMLResponse)
async def time_entry_form(
    project_key: str,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render the log-time form."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login", status_code=302)
    user = cast("User", user_obj)

    try:
        project = await _project_svc.get_by_key(db, project_key)
    except NotFoundError:
        return JSONResponse({"detail": "Project not found"}, status_code=404)

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
