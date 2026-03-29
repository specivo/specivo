"""Web project pages: list, detail, settings, roadmap."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import get_db
from specivo.services.project_service import ProjectService
from specivo.services.version_service import VersionService
from specivo.web.deps import get_current_user_optional, get_templates

if TYPE_CHECKING:
    from specivo.models.user import User

router = APIRouter(tags=["web-projects"], include_in_schema=False)

_svc = ProjectService()
_version_svc = VersionService()

# Status code → label mapping
_STATUS_LABELS = {1: "active", 5: "closed", 9: "archived"}


@router.get("/projects", response_class=HTMLResponse)
async def projects_list(
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render the project list page."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login", status_code=302)
    user = cast("User", user_obj)

    projects, total = await _svc.list_projects(db, user)

    # Enrich projects with status labels
    project_items = []
    for p in projects:
        project_items.append(
            {
                "project": p,
                "status_label": _STATUS_LABELS.get(p.status, "unknown"),
            }
        )

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/projects/list.html",
        context={
            "user": user,
            "active_page": "projects",
            "projects": project_items,
            "total": total,
        },
    )


@router.get("/projects/{key}", response_class=HTMLResponse)
async def project_detail(
    key: str,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render the project detail/overview page."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login", status_code=302)
    user = cast("User", user_obj)

    from specivo.core.exceptions import NotFoundError

    try:
        project = await _svc.get_by_key(db, key)
    except NotFoundError:
        return JSONResponse({"detail": "Project not found"}, status_code=404)

    members = await _svc.list_members(db, project)
    modules = await _svc.get_modules(db, project)

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
            "status_label": _STATUS_LABELS.get(project.status, "unknown"),
        },
    )


@router.get("/projects/{key}/roadmap", response_class=HTMLResponse)
async def project_roadmap(
    key: str,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
    show: str = Query("open"),
) -> Response:
    """Render the project roadmap page."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login", status_code=302)
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


@router.get("/projects/{key}/settings", response_class=HTMLResponse)
async def project_settings(
    key: str,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render project settings page (admin only)."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login", status_code=302)
    user = cast("User", user_obj)

    if not user.is_admin:
        return JSONResponse({"detail": "Admin access required"}, status_code=403)

    from specivo.core.exceptions import NotFoundError

    try:
        project = await _svc.get_by_key(db, key)
    except NotFoundError:
        return JSONResponse({"detail": "Project not found"}, status_code=404)

    members = await _svc.list_members(db, project)
    modules = await _svc.get_modules(db, project)

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
            "status_label": _STATUS_LABELS.get(project.status, "unknown"),
        },
    )
