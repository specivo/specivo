"""Web search page: unified search across issues and wiki."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import get_db
from specivo.services.project_service import ProjectService
from specivo.services.search_service import SearchService
from specivo.web.deps import get_current_user_optional, get_templates

if TYPE_CHECKING:
    from specivo.models.user import User

router = APIRouter(tags=["web-search"], include_in_schema=False)

_search_svc = SearchService()
_project_svc = ProjectService()


@router.get("/search", response_class=HTMLResponse)
async def search_page(
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
    q: str = Query(""),
    mode: str = Query("keyword"),
    scope: str = Query("all"),
    project_key: str = Query(""),
    offset: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=100),
) -> Response:
    """Render the search page with results."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login", status_code=302)
    user = cast("User", user_obj)

    results: list = []
    total: int = 0

    # Resolve project_key to project_id if provided
    project_id = None
    if project_key:
        from specivo.core.exceptions import NotFoundError

        try:
            project = await _project_svc.get_by_key(db, project_key)
            project_id = project.id
        except NotFoundError:
            pass  # Ignore invalid project key, search globally

    if q.strip():
        try:
            if mode == "semantic":
                results, total = await _search_svc.semantic_search(
                    db, q, project_id=project_id, offset=offset, limit=limit
                )
            elif mode == "hybrid":
                results, total = await _search_svc.hybrid_search(
                    db, q, project_id=project_id, scope=scope, offset=offset, limit=limit
                )
            else:
                results, total = await _search_svc.search(
                    db, q, project_id=project_id, scope=scope, offset=offset, limit=limit
                )
        except Exception:
            # Search may fail if DB features not available (e.g., pgvector)
            results, total = [], 0

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/search.html",
        context={
            "user": user,
            "active_page": "search",
            "query": q,
            "mode": mode,
            "scope": scope,
            "project_key": project_key,
            "results": results,
            "total": total,
            "offset": offset,
            "limit": limit,
        },
    )
