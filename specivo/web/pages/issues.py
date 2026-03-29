"""Web issue pages: list, detail, create/edit forms."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import get_db
from specivo.core.exceptions import NotFoundError
from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.services.issue_service import IssueService
from specivo.services.journal_service import JournalService
from specivo.services.project_service import ProjectService
from specivo.services.saved_filter_service import SavedFilterService
from specivo.web.deps import get_current_user_optional, get_templates

if TYPE_CHECKING:
    from specivo.models.user import User

router = APIRouter(tags=["web-issues"], include_in_schema=False)

_issue_svc = IssueService()
_journal_svc = JournalService()
_project_svc = ProjectService()
_saved_filter_svc = SavedFilterService()


async def _get_lookups(db: AsyncSession) -> dict:
    """Load trackers, statuses, priorities for dropdown options."""
    trackers = (await db.execute(select(Tracker).order_by(Tracker.position))).scalars().all()
    statuses = (await db.execute(select(IssueStatus).order_by(IssueStatus.position))).scalars().all()
    priorities = (
        (await db.execute(select(IssuePriority).where(IssuePriority.active.is_(True)).order_by(IssuePriority.position)))
        .scalars()
        .all()
    )
    return {
        "trackers": list(trackers),
        "statuses": list(statuses),
        "priorities": list(priorities),
    }


@router.get("/projects/{project_key}/issues", response_class=HTMLResponse)
async def issues_list(
    project_key: str,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
    status: str = Query("open"),
    tracker_id: int | None = Query(None),
    assigned_to_id: int | None = Query(None),
    priority_id: int | None = Query(None),
    sort: str = Query("created_at:desc"),
    offset: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=100),
) -> Response:
    """Render the issue list page for a project."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login", status_code=302)
    user = cast("User", user_obj)

    try:
        project = await _project_svc.get_by_key(db, project_key)
    except NotFoundError:
        return JSONResponse({"detail": "Project not found"}, status_code=404)

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

    saved_filters = await _saved_filter_svc.list_for_project(db, user, project.id)
    lookups = await _get_lookups(db)

    # Get project members for assignee dropdown
    members = await _project_svc.list_members(db, project)

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/issues/list.html",
        context={
            "user": user,
            "active_page": "issues",
            "active_project": project,
            "project": project,
            "issues": issues,
            "total": total,
            "saved_filters": saved_filters,
            "members": members,
            "offset": offset,
            "limit": limit,
            "sort": sort,
            "filters": filters,
            **lookups,
        },
    )


@router.get("/projects/{project_key}/issues/new", response_class=HTMLResponse)
async def issue_create_form(
    project_key: str,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render the issue creation form."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login", status_code=302)
    user = cast("User", user_obj)

    try:
        project = await _project_svc.get_by_key(db, project_key)
    except NotFoundError:
        return JSONResponse({"detail": "Project not found"}, status_code=404)

    lookups = await _get_lookups(db)
    members = await _project_svc.list_members(db, project)

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/issues/form.html",
        context={
            "user": user,
            "active_page": "issues",
            "active_project": project,
            "project": project,
            "issue": None,
            "mode": "create",
            "members": members,
            **lookups,
        },
    )


@router.get("/projects/{project_key}/issues/{issue_ref}", response_class=HTMLResponse)
async def issue_detail(
    project_key: str,
    issue_ref: str,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render the issue detail page."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login", status_code=302)
    user = cast("User", user_obj)

    try:
        project = await _project_svc.get_by_key(db, project_key)
    except NotFoundError:
        return JSONResponse({"detail": "Project not found"}, status_code=404)

    try:
        issue = await _issue_svc.get_by_display_key_with_relations(db, issue_ref, user=user)
    except NotFoundError:
        return JSONResponse({"detail": "Issue not found"}, status_code=404)

    journals = await _journal_svc.list_for_issue(db, issue.id, include_private=user.is_admin)
    lookups = await _get_lookups(db)
    members = await _project_svc.list_members(db, project)

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/issues/detail.html",
        context={
            "user": user,
            "active_page": "issues",
            "active_project": project,
            "project": project,
            "issue": issue,
            "journals": journals,
            "members": members,
            **lookups,
        },
    )


@router.get("/projects/{project_key}/issues/{issue_ref}/edit", response_class=HTMLResponse)
async def issue_edit_form(
    project_key: str,
    issue_ref: str,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render the issue edit form."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login", status_code=302)
    user = cast("User", user_obj)

    try:
        project = await _project_svc.get_by_key(db, project_key)
    except NotFoundError:
        return JSONResponse({"detail": "Project not found"}, status_code=404)

    try:
        issue = await _issue_svc.get_by_display_key_with_relations(db, issue_ref, user=user)
    except NotFoundError:
        return JSONResponse({"detail": "Issue not found"}, status_code=404)

    lookups = await _get_lookups(db)
    members = await _project_svc.list_members(db, project)

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/issues/form.html",
        context={
            "user": user,
            "active_page": "issues",
            "active_project": project,
            "project": project,
            "issue": issue,
            "mode": "edit",
            "members": members,
            **lookups,
        },
    )
