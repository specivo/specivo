"""Search API — full-text search across issues and wiki pages (M2.3, M7.2, M7.3)."""

from __future__ import annotations

import json
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import get_db
from specivo.core.exceptions import AnonymousAccessDeniedError, ValidationError
from specivo.core.rate_limit import enforce_rate_limit
from specivo.core.security import (
    ANONYMOUS_SEARCH_MAX_LIMIT,
    ANONYMOUS_SEARCH_MAX_OFFSET,
    ANONYMOUS_SEARCH_MODE,
    ANONYMOUS_SEARCH_RATE_LIMIT,
    get_reader,
)
from specivo.models.project import Project
from specivo.models.user import User
from specivo.schemas.search import SearchFilters, SearchResponse
from specivo.services.project_service import ProjectService
from specivo.services.search_service import SearchService
from specivo.services.security_audit_service import SecurityAuditService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["search"])
_service = SearchService()
_audit_service = SecurityAuditService()
_project_service = ProjectService()

# Bounds for the raw ``metadata`` containment filter, to keep crafted payloads small.
_METADATA_FILTER_MAX_BYTES = 2048
_METADATA_FILTER_MAX_KEYS = 10

# Real-tag filter caps (mirror the web search page).
_TAG_VALUE_MAX = 64
_TAG_MAX = 20

# What a visitor without an account may ask of search. Defined in
# specivo.core.security, which the web search page reads too, so the API's
# bounds and the page's cannot drift apart.
_ANONYMOUS_SEARCH_MODE = ANONYMOUS_SEARCH_MODE
_ANONYMOUS_SEARCH_MAX_LIMIT = ANONYMOUS_SEARCH_MAX_LIMIT
_ANONYMOUS_SEARCH_MAX_OFFSET = ANONYMOUS_SEARCH_MAX_OFFSET


async def _anonymous_search_budget(
    request: Request,
    response: Response,
    user: User = Depends(get_reader),
) -> None:
    """Meter anonymous searches against their own per-IP bucket.

    A dependency of its own rather than a ``rate_limit`` on the route,
    because it must never touch the signed-in buckets: it applies only after
    ``get_reader`` has resolved the caller, and only when that caller turns
    out to be anonymous. ``get_reader`` is the same callable the route
    depends on, so FastAPI resolves it once and both see one principal.

    Search gets a bucket separate from ``anon_read`` because it is the
    expensive route: a full-text query costs far more than fetching one issue,
    so it is metered far more tightly.
    """
    if user.is_anonymous:
        await enforce_rate_limit(request, response, "anon_search", *ANONYMOUS_SEARCH_RATE_LIMIT)


def _clean_tag_names(tag: list[str]) -> list[str]:
    """Normalize repeated ``tag`` params: strip, length-cap, dedupe, clamp count."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in tag:
        for part in raw.split(",") if "," in raw else [raw]:
            name = part.strip()
            if not name or len(name) > _TAG_VALUE_MAX:
                continue
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(name)
            if len(out) >= _TAG_MAX:
                return out
    return out


@router.get("/search/", response_model=SearchResponse)
async def search(
    request: Request,
    q: str = Query("", description="Search query (optional when a metadata filter is given)"),
    project_key: str | None = Query(None, description="Scope to a specific project"),
    project_keys: str | None = Query(None, description="Comma-separated project keys for multi-project search"),
    scope: str = Query("all", pattern="^(all|issues|wiki|comments|attachments)$", description="Search scope"),
    mode: str = Query("keyword", pattern="^(keyword|semantic|hybrid)$", description="Search mode"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    limit: int = Query(25, ge=1, le=100, description="Pagination limit"),
    # Metadata filters
    tracker_id: int | None = Query(None, description="Filter by tracker ID"),
    status_id: int | None = Query(None, description="Filter by status ID"),
    priority_id: int | None = Query(None, description="Filter by priority ID"),
    assigned_to_id: int | None = Query(None, description="Filter by assigned user ID"),
    author_id: int | None = Query(None, description="Filter by author user ID"),
    category_id: int | None = Query(None, description="Filter by category ID"),
    fixed_version_id: int | None = Query(None, description="Filter by version ID"),
    created_after: datetime | None = Query(None, description="Issues created after"),
    created_before: datetime | None = Query(None, description="Issues created before"),
    updated_after: datetime | None = Query(None, description="Issues updated after"),
    updated_before: datetime | None = Query(None, description="Issues updated before"),
    metadata: str | None = Query(None, description="JSONB containment filter (JSON string)"),
    tag: list[str] = Query(default=[], description="Real-tag name(s) to filter by (AND logic)"),  # noqa: B006
    user: User = Depends(get_reader),
    _budget: None = Depends(_anonymous_search_budget),
    db: AsyncSession = Depends(get_db),
) -> SearchResponse:
    """Search across issues and wiki pages.

    Supports three modes:
    - keyword: Full-text search using PostgreSQL tsvector (default)
    - semantic: Vector similarity search using pgvector embeddings
    - hybrid: RRF fusion of keyword + semantic results

    Results are sorted by relevance score (descending).
    Access control enforces per-project visibility rules.

    A visitor without an account gets keyword search only, over a short page,
    and a project filter that names a project they may not read is refused the
    same way one naming no project at all is.
    """
    anonymous = user.is_anonymous
    if anonymous and (
        mode != _ANONYMOUS_SEARCH_MODE or limit > _ANONYMOUS_SEARCH_MAX_LIMIT or offset > _ANONYMOUS_SEARCH_MAX_OFFSET
    ):
        raise AnonymousAccessDeniedError()

    # Resolve project IDs
    project_id: int | None = None
    project_ids: list[int] | None = None

    if project_keys is not None:
        # Multi-project search
        keys = [k.strip() for k in project_keys.split(",") if k.strip()]
        if keys and anonymous:
            # Resolved one at a time through the reader's own project lookup,
            # so a key naming no project and a key naming one this visitor may
            # not read are refused identically. The bulk query below cannot do
            # that: it answers "no results" for the unreadable project and an
            # error for the missing one, and the difference is the disclosure.
            project_ids = [(await _project_service.get_readable_by_key(db, key.upper(), user)).id for key in keys]
        elif keys:
            stmt = select(Project.id).where(Project.key.in_(keys))
            result = await db.execute(stmt)
            project_ids = [row[0] for row in result.all()]
            if not project_ids:
                raise ValidationError(
                    message="None of the specified project keys were found",
                    field="project_keys",
                )
    elif project_key is not None:
        if anonymous:
            project_id = (await _project_service.get_readable_by_key(db, project_key.upper(), user)).id
        else:
            stmt = select(Project.id).where(Project.key == project_key)
            result = await db.execute(stmt)
            pid = result.scalar_one_or_none()
            if pid is None:
                raise ValidationError(
                    message=f"Project with key '{project_key}' not found",
                    field="project_key",
                )
            project_id = pid

    # Build metadata filters
    parsed_metadata: dict | None = None
    if metadata is not None and metadata.strip():
        # Cap the raw payload size before parsing to bound resource use.
        if len(metadata) > _METADATA_FILTER_MAX_BYTES:
            raise ValidationError(message="Metadata filter is too large", field="metadata")
        try:
            loaded = json.loads(metadata)
        except json.JSONDecodeError:
            raise ValidationError(
                message="Invalid JSON in metadata filter",
                field="metadata",
            )
        if not isinstance(loaded, dict):
            raise ValidationError(
                message="Metadata filter must be a JSON object",
                field="metadata",
            )
        if len(loaded) > _METADATA_FILTER_MAX_KEYS:
            raise ValidationError(
                message="Metadata filter has too many keys",
                field="metadata",
            )
        parsed_metadata = loaded

    tag_names = _clean_tag_names(tag)

    filters = SearchFilters(
        tracker_id=tracker_id,
        status_id=status_id,
        priority_id=priority_id,
        assigned_to_id=assigned_to_id,
        author_id=author_id,
        category_id=category_id,
        fixed_version_id=fixed_version_id,
        created_after=created_after,
        created_before=created_before,
        updated_after=updated_after,
        updated_before=updated_before,
        metadata=parsed_metadata,
        tag_names=tag_names or None,
    )

    # Check if any filters are active
    has_filters = any(
        v is not None
        for v in (
            tracker_id,
            status_id,
            priority_id,
            assigned_to_id,
            author_id,
            category_id,
            fixed_version_id,
            created_after,
            created_before,
            updated_after,
            updated_before,
            parsed_metadata,
        )
    ) or bool(tag_names)
    active_filters = filters if has_filters else None

    # Tags attach only to issues and wiki pages; coerce non-taggable scopes.
    if tag_names and scope not in ("issues", "wiki"):
        scope = "all"

    has_query = bool(q.strip())
    if not has_query and active_filters is None:
        raise ValidationError(
            message="Provide a search query or a filter",
            field="q",
        )

    type_counts: dict[str, int] = {}
    if not has_query and tag_names:
        # Tag-only listing — issues + wiki, ordered by recency.
        items, total_count, type_counts = await _service.filter_tagged(
            session=db,
            user=user,
            project_id=project_id,
            project_ids=project_ids,
            scope=scope,
            offset=offset,
            limit=limit,
            filters=active_filters,
        )
    elif not has_query:
        # Metadata/attribute-only listing — no full-text term.
        items, total_count, type_counts = await _service.filter_issues(
            session=db,
            user=user,
            project_id=project_id,
            project_ids=project_ids,
            offset=offset,
            limit=limit,
            filters=active_filters,
        )
    elif mode == "semantic":
        items, total_count = await _service.semantic_search(
            session=db,
            query=q,
            user=user,
            project_id=project_id,
            project_ids=project_ids,
            offset=offset,
            limit=limit,
        )
    elif mode == "hybrid":
        items, total_count, type_counts = await _service.hybrid_search(
            session=db,
            query=q,
            user=user,
            project_id=project_id,
            project_ids=project_ids,
            scope=scope,
            offset=offset,
            limit=limit,
            filters=active_filters,
        )
    else:
        items, total_count, type_counts = await _service.search(
            session=db,
            query=q,
            user=user,
            project_id=project_id,
            project_ids=project_ids,
            scope=scope,
            offset=offset,
            limit=limit,
            filters=active_filters,
        )

    # Audit log the search query. Skipped for anonymous visitors, and this is
    # the guard that matters most: unlike ``log_event``, ``log_search_query``
    # writes unconditionally — it is a core feature, not enterprise-gated — so
    # without this an anonymous crawl would persist one row per request. It is
    # also the only write on this path, which a read-only transaction would
    # otherwise refuse outright.
    if not anonymous:
        try:
            filter_details: dict | None = None
            if has_filters:
                filter_details = {k: v for k, v in filters.model_dump().items() if v is not None}
                # Convert datetime to string for JSON serialization
                for fk, fv in filter_details.items():
                    if isinstance(fv, datetime):
                        filter_details[fk] = fv.isoformat()
            await _audit_service.log_search_query(
                session=db,
                user_id=user.id,
                query=q,
                mode=mode,
                scope=scope,
                filters=filter_details,
                result_count=total_count,
                type_counts=type_counts if mode != "semantic" else None,
                request=request,
            )
        except Exception:
            logger.warning("Failed to log search query audit", exc_info=True)

    return SearchResponse(
        total_count=total_count,
        offset=offset,
        limit=limit,
        items=items,
    )
