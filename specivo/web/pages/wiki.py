"""Web wiki pages: index, show, edit, history, create."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import get_db
from specivo.core.exceptions import NotFoundError
from specivo.services.project_service import ProjectService
from specivo.services.wiki_service import WikiService
from specivo.web.deps import get_current_user_optional, get_templates

if TYPE_CHECKING:
    from specivo.models.user import User

router = APIRouter(tags=["web-wiki"], include_in_schema=False)

_project_svc = ProjectService()
_wiki_svc = WikiService()


@router.get("/projects/{project_key}/wiki", response_class=HTMLResponse)
async def wiki_index(
    project_key: str,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render the wiki index page listing all pages."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login", status_code=302)
    user = cast("User", user_obj)

    try:
        project = await _project_svc.get_by_key(db, project_key)
    except NotFoundError:
        return JSONResponse({"detail": "Project not found"}, status_code=404)

    pages = await _wiki_svc.list_pages(db, project.id)

    # Build tree structure: group pages by parent_id
    page_tree: dict[int | None, list] = {}
    for p in pages:
        page_tree.setdefault(p.parent_id, []).append(p)

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/wiki/index.html",
        context={
            "user": user,
            "active_page": "wiki",
            "active_project": project,
            "project": project,
            "pages": pages,
            "page_tree": page_tree,
        },
    )


@router.get("/projects/{project_key}/wiki/new", response_class=HTMLResponse)
async def wiki_new(
    project_key: str,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render the wiki page creation form."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login", status_code=302)
    user = cast("User", user_obj)

    try:
        project = await _project_svc.get_by_key(db, project_key)
    except NotFoundError:
        return JSONResponse({"detail": "Project not found"}, status_code=404)

    pages = await _wiki_svc.list_pages(db, project.id)

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/wiki/edit.html",
        context={
            "user": user,
            "active_page": "wiki",
            "active_project": project,
            "project": project,
            "wiki_page": None,
            "content": None,
            "pages": pages,
            "mode": "create",
        },
    )


@router.get("/projects/{project_key}/wiki/{slug}", response_class=HTMLResponse)
async def wiki_show(
    project_key: str,
    slug: str,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render a wiki page with its content."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login", status_code=302)
    user = cast("User", user_obj)

    try:
        project = await _project_svc.get_by_key(db, project_key)
    except NotFoundError:
        return JSONResponse({"detail": "Project not found"}, status_code=404)

    try:
        page, content = await _wiki_svc.get_page(db, project.id, slug)
    except NotFoundError:
        return JSONResponse({"detail": "Wiki page not found"}, status_code=404)

    all_pages = await _wiki_svc.list_pages(db, project.id)

    # Build tree structure for sidebar
    page_tree: dict[int | None, list] = {}
    for p in all_pages:
        page_tree.setdefault(p.parent_id, []).append(p)

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/wiki/show.html",
        context={
            "user": user,
            "active_page": "wiki",
            "active_project": project,
            "project": project,
            "wiki_page": page,
            "content": content,
            "pages": all_pages,
            "page_tree": page_tree,
        },
    )


@router.get("/projects/{project_key}/wiki/{slug}/edit", response_class=HTMLResponse)
async def wiki_edit(
    project_key: str,
    slug: str,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render the wiki page edit form."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login", status_code=302)
    user = cast("User", user_obj)

    try:
        project = await _project_svc.get_by_key(db, project_key)
    except NotFoundError:
        return JSONResponse({"detail": "Project not found"}, status_code=404)

    try:
        page, content = await _wiki_svc.get_page(db, project.id, slug)
    except NotFoundError:
        return JSONResponse({"detail": "Wiki page not found"}, status_code=404)

    all_pages = await _wiki_svc.list_pages(db, project.id)

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/wiki/edit.html",
        context={
            "user": user,
            "active_page": "wiki",
            "active_project": project,
            "project": project,
            "wiki_page": page,
            "content": content,
            "pages": all_pages,
            "mode": "edit",
        },
    )


@router.get("/projects/{project_key}/wiki/{slug}/history", response_class=HTMLResponse)
async def wiki_history(
    project_key: str,
    slug: str,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render the wiki page history (all versions)."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login", status_code=302)
    user = cast("User", user_obj)

    try:
        project = await _project_svc.get_by_key(db, project_key)
    except NotFoundError:
        return JSONResponse({"detail": "Project not found"}, status_code=404)

    try:
        page, content = await _wiki_svc.get_page(db, project.id, slug)
    except NotFoundError:
        return JSONResponse({"detail": "Wiki page not found"}, status_code=404)

    versions = await _wiki_svc.get_page_history(db, page.id)

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/wiki/history.html",
        context={
            "user": user,
            "active_page": "wiki",
            "active_project": project,
            "project": project,
            "wiki_page": page,
            "content": content,
            "versions": versions,
        },
    )
