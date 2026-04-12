"""Web sprint pages: backlog, board, edit, and analytics views."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from specivo.core.database import get_db
from specivo.core.exceptions import NotFoundError
from specivo.models.issue import Issue
from specivo.models.lookups import IssueStatus
from specivo.services.permission_service import Permission, check_permission
from specivo.services.project_service import ProjectService
from specivo.services.sprint_service import SprintService
from specivo.web.deps import get_current_user_optional, get_templates

if TYPE_CHECKING:
    from specivo.models.user import User

router = APIRouter(tags=["web-sprints"], include_in_schema=False)

_project_svc = ProjectService()
_sprint_svc = SprintService()


@router.get("/projects/{project_key}/sprints/", response_class=HTMLResponse)
async def sprints_list(
    project_key: str,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render the sprints list page showing all sprints grouped by status."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login/", status_code=302)
    user = cast("User", user_obj)

    try:
        project = await _project_svc.get_by_key(db, project_key)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Project not found")
    await _project_svc.require_project_access(db, project, user)

    all_sprints = await _sprint_svc.list_for_project(db, project.id)

    # Group by status
    active_sprints = [s for s in all_sprints if s.status == "active"]
    planned_sprints = [s for s in all_sprints if s.status == "planned"]
    completed_sprints = [s for s in all_sprints if s.status == "completed"]

    # Get issue counts per sprint
    sprint_issue_counts: dict[int, dict] = {}
    for sprint in all_sprints:
        total = await _sprint_svc.sprint_issue_count(db, sprint.id)
        # For completed sprints, use velocity_snapshot
        if sprint.status == "completed" and sprint.velocity_snapshot:
            completed = sprint.velocity_snapshot.get("completed_issues", 0)
            committed = sprint.velocity_snapshot.get("total_issues", total)
        else:
            # For active/planned, count done issues
            board = await _sprint_svc.board_data(db, sprint)
            done_statuses = {"Resolved", "Closed"}
            completed = sum(len(issues) for name, issues in board.items() if name in done_statuses)
            committed = total
        sprint_issue_counts[sprint.id] = {
            "total": committed,
            "completed": completed,
            "remaining": committed - completed,
        }

    # Compute average velocity from completed sprints
    velocities = []
    for s in completed_sprints:
        if s.velocity_snapshot:
            velocities.append(s.velocity_snapshot.get("completed_issues", 0))
    avg_velocity = round(sum(velocities) / len(velocities), 1) if velocities else 0

    can_manage = user.is_admin or await check_permission(user, project.id, Permission.MANAGE_SPRINTS, db)

    # Get active sprint ID for sidebar
    active_sprint_id = active_sprints[0].id if active_sprints else None

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/projects/sprints_list.html",
        context={
            "user": user,
            "active_page": "sprints",
            "active_project": project,
            "project": project,
            "active_sprints": active_sprints,
            "planned_sprints": planned_sprints,
            "completed_sprints": completed_sprints,
            "sprint_issue_counts": sprint_issue_counts,
            "avg_velocity": avg_velocity,
            "can_manage_sprint": can_manage,
            "active_sprint_id": active_sprint_id,
            "today": date.today(),
        },
    )


@router.get("/projects/{project_key}/backlog/", response_class=HTMLResponse)
async def sprint_backlog(
    project_key: str,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
    offset: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=100),
) -> Response:
    """Render the sprint backlog page."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login/", status_code=302)
    user = cast("User", user_obj)

    try:
        project = await _project_svc.get_by_key(db, project_key)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Project not found")
    await _project_svc.require_project_access(db, project, user)

    # Load all sprints for this project
    sprints = await _sprint_svc.list_for_project(db, project.id)

    active_sprint = None
    planned_sprints = []
    completed_sprints = []
    for s in sprints:
        if s.status == "active":
            active_sprint = s
        elif s.status == "planned":
            planned_sprints.append(s)
        else:
            completed_sprints.append(s)

    # Get issue counts per sprint
    sprint_issue_counts: dict[int, int] = {}
    for s in sprints:
        sprint_issue_counts[s.id] = await _sprint_svc.sprint_issue_count(db, s.id)

    # Active sprint stats: completed count and days left
    completed_count = 0
    total_count = 0
    days_left = 0
    if active_sprint:
        total_count = sprint_issue_counts.get(active_sprint.id, 0)
        # Count completed issues (status category in done/closed)
        stmt = (
            select(func.count(Issue.id))
            .join(IssueStatus, Issue.status_id == IssueStatus.id)
            .where(
                Issue.sprint_id == active_sprint.id,
                IssueStatus.category.in_(("done", "closed")),
            )
        )
        result = await db.execute(stmt)
        completed_count = result.scalar_one()
        # Days left
        if active_sprint.end_date:
            delta = active_sprint.end_date - date.today()
            days_left = max(0, delta.days)

    # Load project members for assignee picker
    members = await _project_svc.list_members(db, project)

    # Load backlog issues (sprint_id IS NULL) with pagination
    backlog_issues, backlog_total = await _sprint_svc.backlog_issues(db, project.id, offset=offset, limit=limit)

    # Collect all sprints available for the sprint picker (active + planned)
    all_available_sprints = []
    if active_sprint:
        all_available_sprints.append(active_sprint)
    all_available_sprints.extend(planned_sprints)

    # Velocity data for completed sprints
    completed_sprints_with_velocity = [
        {
            "name": s.name,
            "total_issues": (s.velocity_snapshot or {}).get("total_issues", 0),
            "completed_issues": (s.velocity_snapshot or {}).get("completed_issues", 0),
        }
        for s in completed_sprints
    ]
    average_velocity = await _sprint_svc.average_velocity(db, project.id)

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/projects/sprint_backlog.html",
        context={
            "user": user,
            "active_page": "backlog",
            "active_project": project,
            "project": project,
            "active_sprint": active_sprint,
            "planned_sprints": planned_sprints,
            "completed_sprints": completed_sprints,
            "sprint_issue_counts": sprint_issue_counts,
            "backlog_issues": backlog_issues,
            "backlog_total": backlog_total,
            "offset": offset,
            "limit": limit,
            "members": members,
            "completed_count": completed_count,
            "total_count": total_count,
            "days_left": days_left,
            "all_available_sprints": all_available_sprints,
            "active_sprint_id": active_sprint.id if active_sprint else None,
            "completed_sprints_with_velocity": completed_sprints_with_velocity,
            "average_velocity": average_velocity,
        },
    )


@router.get("/projects/{project_key}/sprints/{sprint_id}/board/", response_class=HTMLResponse)
async def sprint_board(
    project_key: str,
    sprint_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
    board_per_col: int = Query(10, ge=1, le=50),
) -> Response:
    """Render the sprint board page."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login/", status_code=302)
    user = cast("User", user_obj)

    try:
        project = await _project_svc.get_by_key(db, project_key)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Project not found")
    await _project_svc.require_project_access(db, project, user)

    try:
        sprint = await _sprint_svc.get_by_id(db, sprint_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Sprint not found")

    if sprint.project_id != project.id:
        raise HTTPException(status_code=404, detail="Sprint not found in this project")

    # Load statuses for column ordering
    statuses = (
        (await db.execute(select(IssueStatus).order_by(IssueStatus.position)))
        .scalars()
        .all()
    )

    # Load board data grouped by status name
    board = await _sprint_svc.board_data(db, sprint)

    # Parse per-column offsets from query params (col_<status_id>_offset=N)
    col_offsets: dict[int, int] = {}
    for st in statuses:
        param = request.query_params.get(f"col_{st.id}_offset")
        if param is not None:
            try:
                col_offsets[st.id] = max(0, int(param))
            except ValueError:
                pass

    # Build base params string for column pagination URLs
    base_parts = [f"board_per_col={board_per_col}"]
    board_base_params = "&".join(base_parts)

    # Count total issues across all columns
    total_issues = sum(len(v) for v in board.values())

    # Load other sprints for the "move to" option on complete
    all_sprints = await _sprint_svc.list_for_project(db, project.id)
    other_sprints = [s for s in all_sprints if s.id != sprint.id and s.status == "planned"]

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/projects/sprint_board.html",
        context={
            "user": user,
            "active_page": "sprint_board",
            "active_project": project,
            "project": project,
            "sprint": sprint,
            "statuses": list(statuses),
            "board": board,
            "other_sprints": other_sprints,
            "board_per_col": board_per_col,
            "col_offsets": col_offsets,
            "board_base_params": board_base_params,
            "total_issues": total_issues,
            "can_manage_sprint": (
                user.is_admin
                or await check_permission(user, project.id, Permission.MANAGE_SPRINTS, db)
            ),
            "active_sprint_id": sprint.id if sprint.status == "active" else None,
        },
    )


@router.get("/projects/{project_key}/sprints/{sprint_id}/edit/", response_class=HTMLResponse)
async def sprint_edit(
    project_key: str,
    sprint_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render the sprint edit page."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login/", status_code=302)
    user = cast("User", user_obj)

    try:
        project = await _project_svc.get_by_key(db, project_key)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Project not found")
    await _project_svc.require_project_access(db, project, user)

    can_manage = user.is_admin or await check_permission(user, project.id, Permission.MANAGE_SPRINTS, db)
    if not can_manage:
        raise HTTPException(status_code=403, detail="Permission denied")

    try:
        sprint = await _sprint_svc.get_by_id(db, sprint_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Sprint not found")

    if sprint.project_id != project.id:
        raise HTTPException(status_code=404, detail="Sprint not found in this project")

    # Load issues in this sprint with status and tracker eagerly loaded
    stmt = (
        select(Issue)
        .where(Issue.sprint_id == sprint.id)
        .options(
            selectinload(Issue.status),
            selectinload(Issue.tracker),
        )
        .order_by(Issue.id.asc())
    )
    result = await db.execute(stmt)
    issues_in_sprint = list(result.scalars().all())

    total_issue_count = len(issues_in_sprint)
    completed_issue_count = sum(
        1 for i in issues_in_sprint if i.status.category in ("done", "closed")
    )

    members = await _project_svc.list_members(db, project)

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/projects/sprint_edit.html",
        context={
            "user": user,
            "active_page": "backlog",
            "active_project": project,
            "project": project,
            "sprint": sprint,
            "issues_in_sprint": issues_in_sprint,
            "completed_issue_count": completed_issue_count,
            "total_issue_count": total_issue_count,
            "members": members,
        },
    )


@router.get("/projects/{project_key}/sprints/analytics/", response_class=HTMLResponse)
async def sprint_analytics(
    project_key: str,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render the sprint analytics page with velocity charts and history."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login/", status_code=302)
    user = cast("User", user_obj)

    try:
        project = await _project_svc.get_by_key(db, project_key)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Project not found")
    await _project_svc.require_project_access(db, project, user)

    all_sprints = await _sprint_svc.list_for_project(db, project.id)

    completed_sprints = [s for s in all_sprints if s.status == "completed"]
    active_sprints = [s for s in all_sprints if s.status == "active"]

    # Build velocity data from completed sprints
    velocity_data: list[dict] = []
    total_completed_issues = 0
    total_committed_issues = 0
    total_duration_days = 0
    sprints_with_duration = 0

    for sprint in completed_sprints:
        snap = sprint.velocity_snapshot or {}
        committed = snap.get("total_issues", 0)
        completed = snap.get("completed_issues", 0)

        # Compute duration in days
        duration = 0
        if sprint.start_date and sprint.end_date:
            duration = (sprint.end_date - sprint.start_date).days
            total_duration_days += duration
            sprints_with_duration += 1

        completion_rate = round(completed * 100 / committed) if committed > 0 else 0

        velocity_data.append({
            "name": sprint.name,
            "committed": committed,
            "completed": completed,
            "completion_rate": completion_rate,
            "duration": duration,
            "start_date": sprint.start_date.isoformat() if sprint.start_date else None,
            "end_date": sprint.end_date.isoformat() if sprint.end_date else None,
            "sprint_id": sprint.id,
        })

        total_completed_issues += completed
        total_committed_issues += committed

    # Add active sprint with current progress
    for sprint in active_sprints:
        board = await _sprint_svc.board_data(db, sprint)
        done_statuses = {"Resolved", "Closed"}
        completed = sum(
            len(issues) for name, issues in board.items() if name in done_statuses
        )
        committed = sum(len(issues) for issues in board.values())

        duration = 0
        if sprint.start_date and sprint.end_date:
            duration = (sprint.end_date - sprint.start_date).days

        completion_rate = round(completed * 100 / committed) if committed > 0 else 0

        velocity_data.append({
            "name": sprint.name,
            "committed": committed,
            "completed": completed,
            "completion_rate": completion_rate,
            "duration": duration,
            "start_date": sprint.start_date.isoformat() if sprint.start_date else None,
            "end_date": sprint.end_date.isoformat() if sprint.end_date else None,
            "sprint_id": sprint.id,
            "active": True,
        })

    # Compute summary stats (from completed sprints only)
    num_completed = len(completed_sprints)
    avg_velocity = round(total_completed_issues / num_completed, 1) if num_completed else 0
    avg_committed = (
        round(total_committed_issues / num_completed, 1) if num_completed else 0
    )
    avg_completion_rate = (
        round(total_completed_issues * 100 / total_committed_issues)
        if total_committed_issues > 0
        else 0
    )
    avg_duration = (
        round(total_duration_days / sprints_with_duration, 1)
        if sprints_with_duration > 0
        else 0
    )

    # Get active sprint ID for sidebar
    active_sprint_id = active_sprints[0].id if active_sprints else None

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/projects/sprint_analytics.html",
        context={
            "user": user,
            "active_page": "sprints",
            "active_project": project,
            "project": project,
            "velocity_data": velocity_data,
            "avg_velocity": avg_velocity,
            "avg_committed": avg_committed,
            "num_completed": num_completed,
            "avg_completion_rate": avg_completion_rate,
            "avg_duration": avg_duration,
            "active_sprint_id": active_sprint_id,
        },
    )
