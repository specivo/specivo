"""Web search page: unified search across issues and wiki."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, cast

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import get_db
from specivo.schemas.search import SearchFilters
from specivo.services.project_service import ProjectService
from specivo.services.search_service import SearchService
from specivo.services.security_audit_service import SecurityAuditService
from specivo.web.deps import get_current_user_optional, get_templates

# Metadata filter slug charset and value length cap (defense against crafted input).
_MF_SLUG_RE = re.compile(r"^[a-z0-9_-]{1,64}$")
_MV_MAX_LEN = 255

if TYPE_CHECKING:
    from specivo.models.user import User

router = APIRouter(tags=["web-search"], include_in_schema=False)

_search_svc = SearchService()
_project_svc = ProjectService()
_audit_svc = SecurityAuditService()


@router.get("/search/", response_class=HTMLResponse)
async def search_page(
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
    q: str = Query(""),
    mode: str = Query("hybrid"),
    scope: str = Query("all"),
    project_key: str = Query(""),
    mf: str = Query("", description="Metadata field slug to filter by"),
    mv: str = Query("", description="Metadata value to filter by"),
    ma: int = Query(0, description="1 if the metadata field is array-typed"),
    offset: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=100),
) -> Response:
    """Render the search page with results."""
    user_obj = await get_current_user_optional(request, db)
    if not user_obj:
        return RedirectResponse("/login/", status_code=302)
    user = cast("User", user_obj)

    results: list = []
    total: int = 0
    type_counts: dict[str, int] = {}

    # Resolve project_key to project_id if provided
    project_id = None
    if project_key:
        from specivo.core.exceptions import NotFoundError

        try:
            project = await _project_svc.get_by_key(db, project_key)
            await _project_svc.require_project_access(db, project, user)
            project_id = project.id
        except NotFoundError:
            pass  # Ignore invalid project key, search globally

    # Build an optional metadata containment filter from mf/mv/ma. Invalid or
    # incomplete pairs are ignored (treated as "no filter").
    active_filters = None
    mf_clean = mf.strip()
    mv_clean = mv.strip()
    if mf_clean and mv_clean and _MF_SLUG_RE.match(mf_clean) and len(mv_clean) <= _MV_MAX_LEN:
        containment = {mf_clean: [mv_clean]} if ma else {mf_clean: mv_clean}
        active_filters = SearchFilters(metadata=containment)
    else:
        # Drop unusable filter inputs so the template doesn't show a stale chip.
        mf_clean = mv_clean = ""

    if q.strip():
        search_fn = _search_svc.hybrid_search if mode == "hybrid" else _search_svc.search
        try:
            results, total, type_counts = await search_fn(
                db, q, user=user, project_id=project_id, scope=scope, offset=offset, limit=limit, filters=active_filters
            )
        except Exception:
            import logging

            logging.getLogger(__name__).exception("Search failed for q=%r scope=%s mode=%s", q, scope, mode)
            results, total, type_counts = [], 0, {}
    elif active_filters is not None:
        # Metadata-only listing: force the issues scope (only issues carry metadata).
        scope = "issues"
        try:
            results, total, type_counts = await _search_svc.filter_issues(
                db, user=user, project_id=project_id, offset=offset, limit=limit, filters=active_filters
            )
        except Exception:
            import logging

            logging.getLogger(__name__).exception("Metadata filter failed for mf=%r mv=%r", mf_clean, mv_clean)
            results, total, type_counts = [], 0, {}

    # Audit log the search/filter query
    if q.strip() or active_filters is not None:
        try:
            await _audit_svc.log_search_query(
                session=db,
                user_id=user.id,
                query=q,
                mode=mode,
                scope=scope,
                result_count=total,
                type_counts=type_counts or None,
                request=request,
            )
        except Exception:
            pass

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
            "mf": mf_clean,
            "mv": mv_clean,
            "ma": ma,
            "results": results,
            "total": total,
            "offset": offset,
            "limit": limit,
            "type_counts": type_counts,
        },
    )
