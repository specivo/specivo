"""Wiki API — CRUD, versioning, rename with redirects."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import get_db
from specivo.core.exceptions import PermissionDeniedError
from specivo.core.security import get_current_user
from specivo.models.project import EnabledModule
from specivo.models.user import User
from specivo.models.wiki import WikiContent, WikiPage
from specivo.schemas.common import IdName
from specivo.schemas.wiki import (
    WikiContentVersionOut,
    WikiGraphEdge,
    WikiGraphNode,
    WikiGraphResponse,
    WikiPageCreate,
    WikiPageListResponse,
    WikiPageOut,
    WikiPageRename,
    WikiPageUpdate,
    WikiPageWithContent,
    WikiVersionsResponse,
)
from specivo.services.permission_service import check_permission
from specivo.services.project_service import ProjectService
from specivo.services.wiki_link_service import WikiLinkService
from specivo.services.wiki_service import WikiService

router = APIRouter(tags=["wiki"])
_service = WikiService()
_link_service = WikiLinkService()
_project_service = ProjectService()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _require_wiki_module(project_id: int, db: AsyncSession) -> None:
    """Raise 403 if the wiki module is not enabled for the project."""
    stmt = select(EnabledModule).where(
        EnabledModule.project_id == project_id,
        EnabledModule.name == "wiki",
    )
    result = await db.execute(stmt)
    if result.scalar_one_or_none() is None:
        raise PermissionDeniedError("Wiki module is not enabled for this project")


async def _require_view_wiki(user: User, project_id: int, db: AsyncSession) -> None:
    """Raise 403 if the user lacks view_wiki permission."""
    if not await check_permission(user, project_id, "view_wiki", db):
        raise PermissionDeniedError("You do not have permission to view wiki pages")


async def _require_manage_wiki(user: User, project_id: int, db: AsyncSession) -> None:
    """Raise 403 if the user lacks manage_wiki permission."""
    if not await check_permission(user, project_id, "manage_wiki", db):
        raise PermissionDeniedError("You do not have permission to manage wiki pages")


def _page_with_content(page: WikiPage, content: WikiContent) -> WikiPageWithContent:
    """Build a WikiPageWithContent response from models."""
    return WikiPageWithContent(
        id=page.id,
        title=page.title,
        slug=page.slug,
        parent_id=page.parent_id,
        protected=page.protected,
        lock_version=page.lock_version,
        created_at=page.created_at,
        updated_at=page.updated_at,
        text=content.text,
        content_version=content.version,
        content_author=IdName(id=content.author_id, name=content.author.display_name),
        content_updated_at=content.created_at,
    )


def _page_out(page: WikiPage) -> WikiPageOut:
    return WikiPageOut(
        id=page.id,
        title=page.title,
        slug=page.slug,
        parent_id=page.parent_id,
        protected=page.protected,
        lock_version=page.lock_version,
        created_at=page.created_at,
        updated_at=page.updated_at,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/projects/{project_key}/wiki/",
    response_model=WikiPageListResponse,
)
async def list_wiki_pages(
    project_key: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WikiPageListResponse:
    """List all wiki pages for a project."""
    project = await _project_service.get_by_key(db, project_key.upper())
    await _require_wiki_module(project.id, db)
    await _require_view_wiki(current_user, project.id, db)

    pages = await _service.list_pages(db, project.id)
    return WikiPageListResponse(items=[_page_out(p) for p in pages])


@router.post(
    "/projects/{project_key}/wiki/",
    response_model=WikiPageWithContent,
    status_code=status.HTTP_201_CREATED,
)
async def create_wiki_page(
    project_key: str,
    data: WikiPageCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WikiPageWithContent:
    """Create a new wiki page."""
    project = await _project_service.get_by_key(db, project_key.upper())
    await _require_wiki_module(project.id, db)
    await _require_manage_wiki(current_user, project.id, db)

    page, content = await _service.create_page(
        session=db,
        project_id=project.id,
        title=data.title,
        text=data.text,
        author=current_user,
        parent_slug=data.parent_slug,
        comments=data.comments,
    )
    # Reload author for response
    content = await _service.get_page_version(db, page.id, content.version)
    return _page_with_content(page, content)


@router.get(
    "/projects/{project_key}/wiki/graph/",
    response_model=WikiGraphResponse,
)
async def get_wiki_graph(
    project_key: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WikiGraphResponse:
    """Get the link graph for the project wiki."""
    project = await _project_service.get_by_key(db, project_key.upper())
    await _require_wiki_module(project.id, db)
    await _require_view_wiki(current_user, project.id, db)

    wiki = await _service.get_wiki(db, project.id)
    if wiki is None:
        return WikiGraphResponse(nodes=[], edges=[])
    graph = await _link_service.get_link_graph(db, wiki.id)

    return WikiGraphResponse(
        nodes=[WikiGraphNode(**n) for n in graph["nodes"]],
        edges=[WikiGraphEdge(**e) for e in graph["edges"]],
    )


@router.get(
    "/projects/{project_key}/wiki/{slug}/",
    response_model=WikiPageWithContent,
)
async def get_wiki_page(
    project_key: str,
    slug: str,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WikiPageWithContent:
    """Get a wiki page by slug (follows redirects)."""
    project = await _project_service.get_by_key(db, project_key.upper())
    await _require_wiki_module(project.id, db)
    await _require_view_wiki(current_user, project.id, db)

    page, content = await _service.get_page(db, project.id, slug)

    # Audit log the resource view
    try:
        from specivo.services.security_audit_service import SecurityAuditService

        _audit_service = SecurityAuditService()
        await _audit_service.log_resource_viewed(
            session=db,
            user_id=current_user.id,
            resource="wiki_page",
            resource_key=page.slug,
            resource_id=page.id,
            project_id=project.id,
            request=request,
        )
    except Exception:
        import logging

        logging.getLogger(__name__).warning("Failed to log wiki view audit", exc_info=True)

    return _page_with_content(page, content)


@router.patch(
    "/projects/{project_key}/wiki/{slug}/",
    response_model=WikiPageWithContent,
)
async def update_wiki_page(
    project_key: str,
    slug: str,
    data: WikiPageUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WikiPageWithContent:
    """Update wiki page content, title, and/or parent."""
    project = await _project_service.get_by_key(db, project_key.upper())
    await _require_wiki_module(project.id, db)
    await _require_manage_wiki(current_user, project.id, db)

    # Resolve the page first
    page, _old_content = await _service.get_page(db, project.id, slug)

    # Rename if title changed
    if data.title and data.title != page.title:
        page = await _service.rename_page(db, page.id, data.title, data.lock_version)
        # lock_version was bumped by rename — refresh for update_page
        data.lock_version = page.lock_version

    # Update parent if changed
    if data.parent_slug is not None:
        new_parent_id: int | None = None
        if data.parent_slug:
            parent_page = await _service.get_page(db, project.id, data.parent_slug)
            new_parent_id = parent_page[0].id
        if new_parent_id != page.parent_id:
            page.parent_id = new_parent_id
            await db.flush()
            await db.refresh(page)
            data.lock_version = page.lock_version

    page, content = await _service.update_page(
        session=db,
        page_id=page.id,
        text=data.text,
        author=current_user,
        lock_version=data.lock_version,
        comment=data.comments,
    )
    # Reload content with author
    content = await _service.get_page_version(db, page.id, content.version)
    return _page_with_content(page, content)


@router.delete(
    "/projects/{project_key}/wiki/{slug}/",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_wiki_page(
    project_key: str,
    slug: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a wiki page."""
    project = await _project_service.get_by_key(db, project_key.upper())
    await _require_wiki_module(project.id, db)
    await _require_manage_wiki(current_user, project.id, db)

    page, _ = await _service.get_page(db, project.id, slug)
    await _service.delete_page(db, page.id)


@router.post(
    "/projects/{project_key}/wiki/{slug}/rename/",
    response_model=WikiPageWithContent,
)
async def rename_wiki_page(
    project_key: str,
    slug: str,
    data: WikiPageRename,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WikiPageWithContent:
    """Rename a wiki page (creates a redirect from the old slug)."""
    project = await _project_service.get_by_key(db, project_key.upper())
    await _require_wiki_module(project.id, db)
    await _require_manage_wiki(current_user, project.id, db)

    page, _ = await _service.get_page(db, project.id, slug)
    page = await _service.rename_page(db, page.id, data.title, data.lock_version)

    # Get latest content for the response
    _, content = await _service.get_page(db, project.id, page.slug)
    return _page_with_content(page, content)


@router.get(
    "/projects/{project_key}/wiki/{slug}/versions/",
    response_model=WikiVersionsResponse,
)
async def get_wiki_page_history(
    project_key: str,
    slug: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WikiVersionsResponse:
    """Get version history for a wiki page."""
    project = await _project_service.get_by_key(db, project_key.upper())
    await _require_wiki_module(project.id, db)
    await _require_view_wiki(current_user, project.id, db)

    page, _ = await _service.get_page(db, project.id, slug)
    versions = await _service.get_page_history(db, page.id)

    return WikiVersionsResponse(
        versions=[
            WikiContentVersionOut(
                version=v.version,
                author=IdName(id=v.author_id, name=v.author.display_name),
                comments=v.comments,
                created_at=v.created_at,
                text=v.text,
            )
            for v in versions
        ]
    )


@router.get(
    "/projects/{project_key}/wiki/{slug}/versions/{version_num}/",
    response_model=WikiContentVersionOut,
)
async def get_wiki_page_version(
    project_key: str,
    slug: str,
    version_num: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WikiContentVersionOut:
    """Get a specific content version of a wiki page."""
    project = await _project_service.get_by_key(db, project_key.upper())
    await _require_wiki_module(project.id, db)
    await _require_view_wiki(current_user, project.id, db)

    page, _ = await _service.get_page(db, project.id, slug)
    content = await _service.get_page_version(db, page.id, version_num)

    return WikiContentVersionOut(
        version=content.version,
        author=IdName(id=content.author_id, name=content.author.display_name),
        comments=content.comments,
        created_at=content.created_at,
        text=content.text,
    )
