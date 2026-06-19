"""Web wiki pages: index, show, edit, history, create."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import get_db
from specivo.core.exceptions import NotFoundError
from specivo.services.attachment_service import AttachmentService
from specivo.services.permission_service import check_permission
from specivo.services.project_service import ProjectService
from specivo.services.tag_service import TagService
from specivo.services.wiki_service import WikiService
from specivo.web.deps import get_current_user_optional, get_templates

if TYPE_CHECKING:
    from specivo.models.user import User

router = APIRouter(tags=["web-wiki"], include_in_schema=False)

_project_svc = ProjectService()
_wiki_svc = WikiService()
_attachment_svc = AttachmentService()
_tag_svc = TagService()


def _wiki_tags_to_dicts(tags: list) -> list[dict]:
    """Serialize Tag ORM objects to plain dicts for templates / x-data injection."""
    return [{"id": t.id, "name": t.name, "color": t.color} for t in tags]


@router.get("/projects/{project_key}/wiki/", response_class=HTMLResponse)
async def wiki_index(
    project_key: str,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render the wiki home page, or redirect to it after auto-creation."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login/", status_code=302)
    user = cast("User", user_obj)

    try:
        project = await _project_svc.get_by_key(db, project_key)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Project not found")
    await _project_svc.require_project_access(db, project, user)

    # Ensure Home page exists (auto-create if missing)
    await _wiki_svc.ensure_home_page(db, project.id, user)

    return RedirectResponse(
        f"/projects/{project_key}/wiki/home/",
        status_code=302,
    )


@router.get("/projects/{project_key}/wiki/pages/", response_class=HTMLResponse)
async def wiki_all_pages(
    project_key: str,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render the wiki page listing all pages."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login/", status_code=302)
    user = cast("User", user_obj)

    try:
        project = await _project_svc.get_by_key(db, project_key)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Project not found")
    await _project_svc.require_project_access(db, project, user)

    if not await check_permission(user, project.id, "view_wiki", db, request=request):
        raise HTTPException(status_code=403, detail="Permission denied")

    pages = await _wiki_svc.list_pages(db, project.id)
    can_manage = await check_permission(user, project.id, "manage_wiki", db)

    page_tree = _wiki_svc.build_page_tree(pages)

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
            "can_manage_wiki": can_manage,
        },
    )


@router.get("/projects/{project_key}/wiki/new/", response_class=HTMLResponse)
async def wiki_new(
    project_key: str,
    request: Request,
    parent: int | None = Query(None),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render the wiki page creation form."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login/", status_code=302)
    user = cast("User", user_obj)

    try:
        project = await _project_svc.get_by_key(db, project_key)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Project not found")
    await _project_svc.require_project_access(db, project, user)

    if not await check_permission(user, project.id, "manage_wiki", db, request=request):
        raise HTTPException(status_code=403, detail="Permission denied")

    pages = await _wiki_svc.list_pages(db, project.id)

    page_tree = _wiki_svc.build_page_tree(pages)

    # Resolve parent_id to slug for pre-selecting the tree picker
    preselect_parent_slug = ""
    preselect_parent_title = ""
    if parent is not None:
        for p in pages:
            if p.id == parent:
                preselect_parent_slug = p.slug
                preselect_parent_title = p.title
                break

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
            "page_tree": page_tree,
            "mode": "create",
            "preselect_parent_slug": preselect_parent_slug,
            "preselect_parent_title": preselect_parent_title,
        },
    )


@router.get("/projects/{project_key}/wiki/trash/", response_class=HTMLResponse)
async def wiki_trash(
    project_key: str,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render the wiki trash (deleted pages) view."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login/", status_code=302)
    user = cast("User", user_obj)

    try:
        project = await _project_svc.get_by_key(db, project_key)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Project not found")
    await _project_svc.require_project_access(db, project, user)

    if not await check_permission(user, project.id, "delete_wiki_pages", db):
        raise HTTPException(status_code=403, detail="Permission denied")

    deleted_pages = await _wiki_svc.list_deleted_pages(db, project.id)

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/wiki/trash.html",
        context={
            "user": user,
            "active_page": "wiki",
            "active_project": project,
            "project": project,
            "deleted_pages": deleted_pages,
        },
    )


@router.post("/projects/{project_key}/wiki/{slug}/delete/", response_class=HTMLResponse)
async def wiki_delete(
    project_key: str,
    slug: str,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Handle wiki page deletion (form POST)."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login/", status_code=302)
    user = cast("User", user_obj)

    try:
        project = await _project_svc.get_by_key(db, project_key)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Project not found")
    await _project_svc.require_project_access(db, project, user)

    if not await check_permission(user, project.id, "delete_wiki_pages", db):
        raise HTTPException(status_code=403, detail="Permission denied")

    page, _ = await _wiki_svc.get_page(db, project.id, slug)

    form = await request.form()
    cascade = form.get("cascade_children") == "true"
    await _wiki_svc.delete_page(db, page.id, deleted_by=user, cascade_children=cascade)

    return RedirectResponse(
        f"/projects/{project_key}/wiki/pages/",
        status_code=302,
    )


@router.post("/projects/{project_key}/wiki/trash/{page_id}/restore/", response_class=HTMLResponse)
async def wiki_restore(
    project_key: str,
    page_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Restore a soft-deleted wiki page."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login/", status_code=302)
    user = cast("User", user_obj)

    try:
        project = await _project_svc.get_by_key(db, project_key)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Project not found")
    await _project_svc.require_project_access(db, project, user)

    if not await check_permission(user, project.id, "delete_wiki_pages", db):
        raise HTTPException(status_code=403, detail="Permission denied")

    await _wiki_svc.restore_page(db, page_id)

    return RedirectResponse(
        f"/projects/{project_key}/wiki/trash/",
        status_code=302,
    )


@router.get("/projects/{project_key}/wiki/{slug}/", response_class=HTMLResponse)
async def wiki_show(
    project_key: str,
    slug: str,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render a wiki page with its content."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login/", status_code=302)
    user = cast("User", user_obj)

    try:
        project = await _project_svc.get_by_key(db, project_key)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Project not found")
    await _project_svc.require_project_access(db, project, user)

    if not await check_permission(user, project.id, "view_wiki", db, request=request):
        raise HTTPException(status_code=403, detail="Permission denied")

    try:
        page, content = await _wiki_svc.get_page(db, project.id, slug)
    except NotFoundError:
        if slug == "home":
            await _wiki_svc.ensure_home_page(db, project.id, user)
            page, content = await _wiki_svc.get_page(db, project.id, slug)
        else:
            raise HTTPException(status_code=404, detail="Wiki page not found")

    all_pages = await _wiki_svc.list_pages(db, project.id)
    can_manage = await check_permission(user, project.id, "manage_wiki", db)

    # Build tree structure for sidebar
    page_tree = _wiki_svc.build_page_tree(all_pages)
    parent_map: dict[int, int | None] = {p.id: p.parent_id for p in all_pages}

    # Compute ancestor IDs for auto-expanding the tree to the current page
    expanded_ids: set[int] = set()
    cursor = page.parent_id
    while cursor is not None:
        expanded_ids.add(cursor)
        cursor = parent_map.get(cursor)
    # Also expand the current page itself if it has children
    if page.id in page_tree:
        expanded_ids.add(page.id)

    # Load attachments for the wiki page (serialized for Alpine.js)
    raw_attachments = await _attachment_svc.list_for_container(db, "WikiPage", page.id)
    attachments_json = [
        {
            "id": att.id,
            "filename": att.filename,
            "content_type": att.content_type or "application/octet-stream",
            "filesize": att.filesize,
            "author": {
                "id": att.author_id,
                "name": att.author.display_name or att.author.login,
            },
            "created_at": att.created_at.isoformat() if att.created_at else None,
        }
        for att in raw_attachments
    ]

    # Project-wide filename → download URL map for inline image resolution
    attachment_map = await _attachment_svc.build_project_attachment_map(db, project.id)

    wiki_tags = _wiki_tags_to_dicts(await _tag_svc.tags_for_wiki_page(db, page.id))

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
            "expanded_ids": expanded_ids,
            "can_manage_wiki": can_manage,
            "attachments": attachments_json,
            "attachment_map": attachment_map,
            "wiki_tags": wiki_tags,
        },
    )


@router.get("/projects/{project_key}/wiki/{slug}/edit/", response_class=HTMLResponse)
async def wiki_edit(
    project_key: str,
    slug: str,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render the wiki page edit form."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login/", status_code=302)
    user = cast("User", user_obj)

    try:
        project = await _project_svc.get_by_key(db, project_key)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Project not found")
    await _project_svc.require_project_access(db, project, user)

    if not await check_permission(user, project.id, "manage_wiki", db, request=request):
        raise HTTPException(status_code=403, detail="Permission denied")

    try:
        page, content = await _wiki_svc.get_page(db, project.id, slug)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Wiki page not found")

    all_pages = await _wiki_svc.list_pages(db, project.id)

    page_tree = _wiki_svc.build_page_tree(all_pages)

    # Current parent slug/title for the tree picker
    current_parent_slug = ""
    current_parent_title = ""
    if page.parent_id is not None:
        for p in all_pages:
            if p.id == page.parent_id:
                current_parent_slug = p.slug
                current_parent_title = p.title
                break

    wiki_tags = _wiki_tags_to_dicts(await _tag_svc.tags_for_wiki_page(db, page.id))

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
            "page_tree": page_tree,
            "mode": "edit",
            "preselect_parent_slug": current_parent_slug,
            "preselect_parent_title": current_parent_title,
            "wiki_tags": wiki_tags,
        },
    )


@router.get("/projects/{project_key}/wiki/{slug}/diff/", response_class=HTMLResponse)
async def wiki_diff(
    project_key: str,
    slug: str,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
    from_version: int = 1,
    to_version: int = 2,
) -> Response:
    """Render a side-by-side diff between two wiki page versions."""
    from difflib import unified_diff

    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login/", status_code=302)
    user = cast("User", user_obj)

    try:
        project = await _project_svc.get_by_key(db, project_key)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Project not found")
    await _project_svc.require_project_access(db, project, user)

    if not await check_permission(user, project.id, "view_wiki", db, request=request):
        raise HTTPException(status_code=403, detail="Permission denied")

    try:
        page, _ = await _wiki_svc.get_page(db, project.id, slug)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Wiki page not found")

    try:
        content_from = await _wiki_svc.get_page_version(db, page.id, from_version)
        content_to = await _wiki_svc.get_page_version(db, page.id, to_version)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Version not found")

    # Generate unified diff lines
    from_lines = (content_from.text or "").splitlines(keepends=True)
    to_lines = (content_to.text or "").splitlines(keepends=True)
    diff_lines = list(
        unified_diff(
            from_lines,
            to_lines,
            fromfile=f"Version {from_version}",
            tofile=f"Version {to_version}",
            lineterm="",
        )
    )

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/wiki/diff.html",
        context={
            "user": user,
            "active_page": "wiki",
            "active_project": project,
            "project": project,
            "wiki_page": page,
            "from_version": from_version,
            "to_version": to_version,
            "diff_lines": diff_lines,
        },
    )


@router.get("/projects/{project_key}/wiki/{slug}/history/", response_class=HTMLResponse)
async def wiki_history(
    project_key: str,
    slug: str,
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Render the wiki page history (all versions)."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login/", status_code=302)
    user = cast("User", user_obj)

    try:
        project = await _project_svc.get_by_key(db, project_key)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Project not found")
    await _project_svc.require_project_access(db, project, user)

    if not await check_permission(user, project.id, "view_wiki", db, request=request):
        raise HTTPException(status_code=403, detail="Permission denied")

    try:
        page, content = await _wiki_svc.get_page(db, project.id, slug)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Wiki page not found")

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
