"""Web project pages: list, detail, settings, roadmap, version detail."""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.constants import DEFAULT_PROJECT_COLORS
from specivo.core.database import get_db
from specivo.models.issue import Issue
from specivo.models.project import Project
from specivo.models.time_entry import TimeEntry
from specivo.services.issue_service import IssueService
from specivo.services.project_service import ProjectService
from specivo.services.version_service import VersionService
from specivo.web.deps import get_current_user_optional, get_templates

if TYPE_CHECKING:
    from specivo.models.user import User

router = APIRouter(tags=["web-projects"], include_in_schema=False)

_svc = ProjectService()
_version_svc = VersionService()
_issue_svc = IssueService()

# Status code -> label mapping
_STATUS_LABELS = {1: "active", 5: "closed", 9: "archived"}


@router.get("/projects/", response_class=HTMLResponse)
async def projects_list(
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render the project list page."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login/", status_code=302)
    user = cast("User", user_obj)

    projects, total = await _svc.list_projects(db, user, limit=500)

    # Build tree: group by parent_id, enrich with status labels.
    # If a child's parent is not in the visible set (private parent the
    # user can't see), the child appears as a root-level project.
    visible_ids = {p.id for p in projects}
    all_project_ids = list(visible_ids)

    # Batch-load stats for all visible projects
    project_stats = await _svc.load_project_stats(db, all_project_ids)

    by_parent: dict[int | None, list] = {}
    for p in projects:
        pstats = project_stats.get(p.id, {})
        open_count = pstats.get("open_count", 0)
        closed_count = pstats.get("closed_count", 0)
        total_issues = open_count + closed_count
        item = {
            "project": p,
            "status_label": _STATUS_LABELS.get(p.status, "unknown"),
            "open_count": open_count,
            "closed_count": closed_count,
            "total_issues": total_issues,
            "closed_pct": round(closed_count / total_issues * 100) if total_issues > 0 else 0,
            "member_count": pstats.get("member_count", 0),
            "wiki_page_count": pstats.get("wiki_page_count", 0),
            "modules": pstats.get("modules", {}),
            "members": pstats.get("members", []),
        }
        # Treat as root if parent is not visible to this user
        effective_parent = p.parent_id if p.parent_id in visible_ids else None
        by_parent.setdefault(effective_parent, []).append(item)

    root_projects = by_parent.get(None, [])

    # Build list of all projects for the parent dropdown in the create modal
    all_projects_for_dropdown = [
        {"key": p.key, "name": p.name}
        for p in sorted(projects, key=lambda x: x.name)
        if p.status == 1  # only active projects
    ]

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/projects/list.html",
        context={
            "user": user,
            "active_page": "projects",
            "projects": root_projects,
            "children_by_parent": by_parent,
            "total": total,
            "project_colors": DEFAULT_PROJECT_COLORS,
            "all_projects": all_projects_for_dropdown,
        },
    )


@router.get("/projects/{key}/", response_class=HTMLResponse)
async def project_detail(
    key: str,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render the project detail/overview page."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login/", status_code=302)
    user = cast("User", user_obj)

    from specivo.core.exceptions import NotFoundError

    try:
        project = await _svc.get_by_key(db, key)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Project not found")
    await _svc.require_project_access(db, project, user)

    member_count = await _svc.count_members(db, project)
    members = await _svc.list_members(db, project, limit=10)
    modules = await _svc.get_modules(db, project)

    # Fetch subprojects
    result = await db.execute(select(Project).where(Project.parent_id == project.id).order_by(Project.name))
    subprojects = result.scalars().all()

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/projects/detail.html",
        context={
            "user": user,
            "active_page": "overview",
            "active_project": project,
            "project": project,
            "members": members,
            "member_count": member_count,
            "modules": modules,
            "subprojects": subprojects,
            "status_label": _STATUS_LABELS.get(project.status, "unknown"),
        },
    )


@router.get("/projects/{key}/roadmap/", response_class=HTMLResponse)
async def project_roadmap(
    key: str,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
    show: str = Query("open"),
) -> Response:
    """Render the project roadmap page."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login/", status_code=302)
    user = cast("User", user_obj)

    from specivo.core.exceptions import NotFoundError

    try:
        project = await _svc.get_by_key(db, key)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Project not found")
    await _svc.require_project_access(db, project, user)

    entries = await _version_svc.roadmap(db, project)

    # Filter by version status if show=open
    if show == "open":
        entries = [e for e in entries if e.version.status == "open"]

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/projects/roadmap.html",
        context={
            "user": user,
            "active_page": "roadmap",
            "active_project": project,
            "project": project,
            "entries": entries,
            "show": show,
        },
    )


@router.get("/projects/{key}/settings/", response_class=HTMLResponse)
async def project_settings(
    key: str,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render project settings page (admin only)."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login/", status_code=302)
    user = cast("User", user_obj)

    from specivo.core.exceptions import NotFoundError
    from specivo.services.permission_service import check_permission

    try:
        project = await _svc.get_by_key(db, key)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Project not found")
    await _svc.require_project_access(db, project, user)

    if not user.is_admin and not await check_permission(user, project.id, "manage_project", db):
        raise HTTPException(status_code=403, detail="Permission denied")

    members = await _svc.list_members(db, project)
    modules = await _svc.get_modules(db, project)

    from sqlalchemy import select

    from specivo.models.role import Role

    roles_result = await db.execute(
        select(Role).where(Role.builtin == 0, Role.assignable.is_(True)).order_by(Role.position)
    )
    roles = [{"id": r.id, "name": r.name} for r in roles_result.scalars().all()]

    # Build list of available parent projects (all projects except self and descendants)
    all_projects_result = await db.execute(select(Project).where(Project.status == 1).order_by(Project.name))
    all_projects = all_projects_result.scalars().all()
    own_path = project.path
    available_parents = [
        {"id": p.id, "key": p.key, "name": p.name}
        for p in all_projects
        if p.id != project.id and not p.path.startswith(own_path + ".")
    ]

    # Check if user can manage versions (separate permission from manage_project)
    can_manage_versions = user.is_admin or await check_permission(user, project.id, "manage_versions", db)

    # Load versions with roadmap data (progress, issue counts)
    roadmap_entries = await _version_svc.roadmap(db, project)
    today = datetime.date.today().isoformat()
    versions_data = []
    for e in roadmap_entries:
        due = e.version.effective_date
        due_str = str(due) if due else ""
        versions_data.append(
            {
                "id": e.version.id,
                "name": e.version.name,
                "description": e.version.description or "",
                "status": e.version.status,
                "due_date": due_str,
                "progress": e.progress_percent,
                "open_count": e.open_count,
                "closed_count": e.closed_count,
                "overdue": bool(due and due_str < today and e.version.status != "closed"),
            }
        )

    # Metadata presets and schemas
    from specivo.models.lookups import Tracker
    from specivo.schemas.metadata_schema import MetadataPresetOut, MetadataSchemaOut
    from specivo.services.metadata_preset_service import MetadataPresetService
    from specivo.services.metadata_schema_service import MetadataSchemaService

    preset_svc = MetadataPresetService()
    schema_svc = MetadataSchemaService()

    all_presets = await preset_svc.list_presets(db)
    presets_data = [MetadataPresetOut.model_validate(p).model_dump(mode="json") for p in all_presets]

    enabled_slugs = await preset_svc.list_enabled(db, project.id)

    project_schemas = await schema_svc.list_for_project(db, project.id)
    schemas_data = []
    for s in project_schemas:
        sd = MetadataSchemaOut.model_validate(s).model_dump(mode="json")
        sd["usage_count"] = await schema_svc.count_usages(db, s)
        schemas_data.append(sd)

    # Trackers / statuses / priorities for scope + recurring-task form dropdowns
    from specivo.models.lookups import IssuePriority, IssueStatus

    trackers_result = await db.execute(select(Tracker).order_by(Tracker.position))
    trackers_data = [{"id": t.id, "name": t.name} for t in trackers_result.scalars().all()]

    statuses_result = await db.execute(select(IssueStatus).order_by(IssueStatus.position))
    statuses_data = [{"id": s.id, "name": s.name} for s in statuses_result.scalars().all()]

    priorities_result = await db.execute(select(IssuePriority).order_by(IssuePriority.position))
    priorities_data = [{"id": p.id, "name": p.name} for p in priorities_result.scalars().all()]

    # Recurring patterns management section (reuses the recurring web helpers)
    from specivo.services.recurring_pattern_service import RecurringPatternService
    from specivo.web.pages.recurring import _pattern_summary

    recurring_svc = RecurringPatternService()
    recurring_patterns = await recurring_svc.list_for_project(db, project.id)
    recurring_patterns_data = [_pattern_summary(p) for p in recurring_patterns]
    can_manage_recurring = user.is_admin or await check_permission(
        user, project.id, "manage_recurring_tasks", db
    )

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/projects/settings.html",
        context={
            "user": user,
            "active_page": "settings",
            "active_project": project,
            "project": project,
            "members": members,
            "modules": modules,
            "roles": roles,
            "versions_data": versions_data,
            "can_manage_versions": can_manage_versions,
            "available_parents": available_parents,
            "status_label": _STATUS_LABELS.get(project.status, "unknown"),
            "presets_data": presets_data,
            "enabled_slugs": enabled_slugs,
            "schemas_data": schemas_data,
            "trackers_data": trackers_data,
            "statuses_data": statuses_data,
            "priorities_data": priorities_data,
            "recurring_patterns_data": recurring_patterns_data,
            "can_manage_recurring": can_manage_recurring,
        },
    )


@router.get("/projects/{key}/versions/{version_id}/", response_class=HTMLResponse)
async def version_detail(
    key: str,
    version_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render the version detail page with issues, progress, and time stats."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login/", status_code=302)
    user = cast("User", user_obj)

    from specivo.core.exceptions import NotFoundError

    try:
        project = await _svc.get_by_key(db, key)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Project not found")
    await _svc.require_project_access(db, project, user)

    try:
        version = await _version_svc.get_by_id(db, version_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Version not found")

    # Verify version belongs to this project
    if version.project_id != project.id:
        raise HTTPException(status_code=404, detail="Version not found")

    # Load roadmap entry for progress/counts
    roadmap_entries = await _version_svc.roadmap(db, project)
    roadmap_entry = next((e for e in roadmap_entries if e.version.id == version.id), None)

    open_count = roadmap_entry.open_count if roadmap_entry else 0
    closed_count = roadmap_entry.closed_count if roadmap_entry else 0
    total_count = roadmap_entry.total if roadmap_entry else 0
    progress_percent = roadmap_entry.progress_percent if roadmap_entry else 0

    # Load issues assigned to this version (all statuses)
    issues, _total = await _issue_svc.list_issues(
        db,
        project_id=project.id,
        filters={"status": "all", "version_id": version.id},
        sort="priority_id:desc,updated_at:desc",
        offset=0,
        limit=500,
        user=user,
    )

    # Build JSON-serializable issue list for Alpine.js
    issues_json = []
    for issue in issues:
        status_name = issue.status.name if issue.status else "Unknown"
        is_closed = issue.status.is_closed if issue.status else False
        tracker_name = issue.tracker.name if issue.tracker else "Unknown"
        priority_name = issue.priority.name if issue.priority else "Normal"
        assignee_name = ""
        assignee_avatar_url = ""
        if issue.assigned_to:
            assignee_name = issue.assigned_to.display_name or issue.assigned_to.login
            assignee_avatar_url = issue.assigned_to.avatar_url or ""

        issues_json.append(
            {
                "key": issue.display_key,
                "url": f"/issue/{issue.display_key}/",
                "subject": issue.subject,
                "tracker": tracker_name,
                "tracker_class": tracker_name.lower().replace(" ", "-"),
                "status": status_name,
                "status_class": status_name.lower().replace(" ", "-"),
                "priority": priority_name.lower().replace(" ", "-"),
                "assignee": assignee_name,
                "assignee_avatar_url": assignee_avatar_url,
                "is_open": not is_closed,
            }
        )

    # Aggregate time logged and estimated hours for this version's issues
    issue_ids = [issue.id for issue in issues]
    time_logged = Decimal(0)
    estimated_hours = Decimal(0)

    if issue_ids:
        # Sum time entries for these issues
        time_result = await db.execute(
            select(func.coalesce(func.sum(TimeEntry.hours), 0)).where(TimeEntry.issue_id.in_(issue_ids))
        )
        time_logged = Decimal(str(time_result.scalar_one()))

        # Sum estimated hours from issues
        est_result = await db.execute(
            select(func.coalesce(func.sum(Issue.estimated_hours), 0)).where(Issue.id.in_(issue_ids))
        )
        estimated_hours = Decimal(str(est_result.scalar_one()))

    # Due date countdown
    today = datetime.date.today()
    due_date = version.effective_date
    days_diff = None
    due_class = "due-ok"
    if due_date:
        days_diff = (due_date - today).days
        if days_diff < 0:
            due_class = "due-overdue"
        elif days_diff <= 7:
            due_class = "due-soon"

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/projects/version_detail.html",
        context={
            "user": user,
            "active_page": "roadmap",
            "active_project": project,
            "project": project,
            "version": version,
            "open_count": open_count,
            "closed_count": closed_count,
            "total_count": total_count,
            "progress_percent": progress_percent,
            "issues_json": issues_json,
            "time_logged": float(time_logged),
            "estimated_hours": float(estimated_hours),
            "days_diff": days_diff,
            "due_class": due_class,
        },
    )
