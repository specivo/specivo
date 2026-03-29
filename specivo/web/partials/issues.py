"""Htmx partials for issue pages — return HTML fragments, not full pages."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import get_db
from specivo.core.exceptions import NotFoundError
from specivo.services.issue_service import IssueService
from specivo.services.journal_service import JournalService
from specivo.services.project_service import ProjectService
from specivo.web.deps import get_current_user_optional, get_templates

if TYPE_CHECKING:
    from specivo.models.user import User

router = APIRouter(prefix="/partials", tags=["web-partials"], include_in_schema=False)

_issue_svc = IssueService()
_journal_svc = JournalService()
_project_svc = ProjectService()


@router.get("/issues/table", response_class=HTMLResponse)
async def issue_table_partial(
    request: Request,
    project_key: str = Query(...),
    db: AsyncSession = Depends(get_db),  # noqa: B008
    status: str = Query("open"),
    tracker_id: int | None = Query(None),
    assigned_to_id: int | None = Query(None),
    priority_id: int | None = Query(None),
    sort: str = Query("created_at:desc"),
    offset: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=100),
) -> Response:
    """Return issue table rows as an HTML fragment for htmx swapping."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login", status_code=302)
    user = cast("User", user_obj)

    try:
        project = await _project_svc.get_by_key(db, project_key)
    except NotFoundError:
        return HTMLResponse("<tr><td colspan='7'>Project not found</td></tr>", status_code=404)

    filters: dict = {"status": status}
    if tracker_id is not None:
        filters["tracker_id"] = tracker_id
    if assigned_to_id is not None:
        filters["assigned_to_id"] = assigned_to_id
    if priority_id is not None:
        filters["priority_id"] = priority_id

    issues, total = await _issue_svc.list_issues(
        db,
        project_id=project.id,
        filters=filters,
        sort=sort,
        offset=offset,
        limit=limit,
        user=user,
    )

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/issues/_table_rows.html",
        context={
            "issues": issues,
            "total": total,
            "project": project,
            "offset": offset,
            "limit": limit,
        },
    )


@router.get("/issues/{issue_ref}/activity", response_class=HTMLResponse)
async def issue_activity_partial(
    issue_ref: str,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Return the activity/journal list as an HTML fragment for htmx swapping."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login", status_code=302)
    user = cast("User", user_obj)

    try:
        issue = await _issue_svc.get_by_display_key(db, issue_ref, user=user)
    except NotFoundError:
        return HTMLResponse("<p>Issue not found</p>", status_code=404)

    journals = await _journal_svc.list_for_issue(db, issue.id, include_private=user.is_admin)

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/issues/_activity.html",
        context={
            "issue": issue,
            "journals": journals,
            "user": user,
        },
    )
