"""Htmx partials for the dashboard — return HTML fragments, not full pages."""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from specivo.core.database import get_db
from specivo.models.issue import Issue
from specivo.models.lookups import IssueStatus
from specivo.models.project import Project
from specivo.models.user import User
from specivo.services.project_service import ProjectService
from specivo.web.deps import get_current_user_optional, get_templates

router = APIRouter(prefix="/partials/dashboard", tags=["web-partials"], include_in_schema=False)

_project_svc = ProjectService()


@router.get("/my-issues/", response_class=HTMLResponse)
async def my_issues_partial(
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
    offset: int = Query(0, ge=0),
    limit: int = Query(10, ge=1, le=50),
) -> Response:
    """Return My Open Issues table as an HTML fragment for htmx swapping."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login/", status_code=302)
    user = cast("User", user_obj)

    projects, _ = await _project_svc.list_projects(db, user, offset=0, limit=50)
    visible_pids = {p.id for p in projects}

    my_issues: list[Issue] = []
    total = 0
    if visible_pids:
        # Count total
        count_stmt = (
            select(func.count())
            .select_from(Issue)
            .join(IssueStatus, Issue.status_id == IssueStatus.id)
            .where(
                Issue.assigned_to_id == user.id,
                IssueStatus.category != "closed",
                Issue.project_id.in_(visible_pids),
            )
        )
        total = (await db.execute(count_stmt)).scalar() or 0

        # Fetch page
        my_issues_stmt = (
            select(Issue)
            .join(IssueStatus, Issue.status_id == IssueStatus.id)
            .where(
                Issue.assigned_to_id == user.id,
                IssueStatus.category != "closed",
                Issue.project_id.in_(visible_pids),
            )
            .options(
                selectinload(Issue.tracker),
                selectinload(Issue.status),
                selectinload(Issue.priority),
            )
            .order_by(Issue.updated_at.desc())
            .offset(offset)
            .limit(limit)
        )
        my_issues = list((await db.execute(my_issues_stmt)).scalars().all())

    # Batch-load project names
    issue_project_ids = list({i.project_id for i in my_issues})
    if issue_project_ids:
        proj_rows = (
            await db.execute(
                select(Project.id, Project.name).where(Project.id.in_(issue_project_ids))
            )
        ).all()
        proj_names = {r.id: r.name for r in proj_rows}
        for issue in my_issues:
            issue.project_name = proj_names.get(issue.project_id, "")  # type: ignore[attr-defined]

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/_my_issues.html",
        context={
            "user": user,
            "my_issues": my_issues,
            "my_issues_total": total,
            "my_issues_offset": offset,
            "my_issues_limit": limit,
        },
    )
