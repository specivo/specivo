"""Web time tracking pages: list, log-time form."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import DB_SESSION
from specivo.models.project import Project
from specivo.services.permission_service import Permission, check_permission
from specivo.services.project_service import ProjectService
from specivo.services.time_entry_service import TimeEntryService
from specivo.web.deps import ProjectReader, WebUser, get_templates

if TYPE_CHECKING:
    pass

router = APIRouter(tags=["web-time"], include_in_schema=False)

_project_svc = ProjectService()
_time_svc = TimeEntryService()


@router.get("/projects/{project_key}/time-entries/", response_class=HTMLResponse)
async def time_entries_list(
    request: Request,
    user: WebUser,
    project: Annotated[Project, Depends(ProjectReader(Permission.VIEW_TIME_ENTRIES))],
    db: AsyncSession = DB_SESSION,
    user_id: str = Query(""),
    from_date: date | None = Query(None),
    to_date: date | None = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=100),
) -> Response:
    """Render the time entries list page for a project."""
    from specivo.core.utils import safe_int

    entries, total = await _time_svc.list_for_project(
        db,
        project,
        user,
        user_id=safe_int(user_id),
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

    # Controls are shown only to users the API would accept (TimeEntryService.can_edit).
    can_log_time = await check_permission(user, project.id, Permission.LOG_TIME, db)
    can_manage_time_entries = await check_permission(user, project.id, Permission.MANAGE_TIME_ENTRIES, db)

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
            "can_log_time": can_log_time,
            "can_manage_time_entries": can_manage_time_entries,
            "offset": offset,
            "limit": limit,
            "filters": {
                "user_id": user_id,
                "from_date": from_date,
                "to_date": to_date,
            },
        },
    )


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
