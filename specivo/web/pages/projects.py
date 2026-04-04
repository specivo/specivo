"""Web project pages: list, detail, settings, roadmap."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.constants import DEFAULT_PROJECT_COLORS
from specivo.core.database import get_db
from specivo.models.project import Project
from specivo.services.project_service import ProjectService
from specivo.services.version_service import VersionService
from specivo.web.deps import get_current_user_optional, get_templates

if TYPE_CHECKING:
    from specivo.models.user import User

router = APIRouter(tags=["web-projects"], include_in_schema=False)

_svc = ProjectService()
_version_svc = VersionService()

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
        return JSONResponse({"detail": "Project not found"}, status_code=404)

    members = await _svc.list_members(db, project)
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
        return JSONResponse({"detail": "Project not found"}, status_code=404)

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
        return JSONResponse({"detail": "Project not found"}, status_code=404)

    if not user.is_admin and not await check_permission(user, project.id, "manage_project", db):
        return JSONResponse({"detail": "Permission denied"}, status_code=403)

    members = await _svc.list_members(db, project)
    modules = await _svc.get_modules(db, project)

    from sqlalchemy import select

    from specivo.models.role import Role

    roles_result = await db.execute(
        select(Role)
        .where(Role.builtin == 0, Role.assignable.is_(True))
        .order_by(Role.position)
    )
    roles = [{"id": r.id, "name": r.name} for r in roles_result.scalars().all()]

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
            "status_label": _STATUS_LABELS.get(project.status, "unknown"),
        },
    )
