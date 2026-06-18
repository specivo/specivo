"""Search API — full-text search across issues and wiki pages (M2.3, M7.2, M7.3)."""

from __future__ import annotations

import json
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import get_db
from specivo.core.exceptions import ValidationError
from specivo.core.security import get_current_user
from specivo.models.project import Project
from specivo.models.user import User
from specivo.schemas.search import SearchFilters, SearchResponse
from specivo.services.search_service import SearchService
from specivo.services.security_audit_service import SecurityAuditService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["search"])
_service = SearchService()
_audit_service = SecurityAuditService()

# Bounds for the raw ``metadata`` containment filter, to keep crafted payloads small.
_METADATA_FILTER_MAX_BYTES = 2048
_METADATA_FILTER_MAX_KEYS = 10


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
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SearchResponse:
    """Search across issues and wiki pages.

    Supports three modes:
    - keyword: Full-text search using PostgreSQL tsvector (default)
    - semantic: Vector similarity search using pgvector embeddings
    - hybrid: RRF fusion of keyword + semantic results

    Results are sorted by relevance score (descending).
    Access control enforces per-project visibility rules.
    """
    # Resolve project IDs
    project_id: int | None = None
    project_ids: list[int] | None = None

    if project_keys is not None:
        # Multi-project search
        keys = [k.strip() for k in project_keys.split(",") if k.strip()]
        if keys:
            stmt = select(Project.id).where(Project.key.in_(keys))
            result = await db.execute(stmt)
            project_ids = [row[0] for row in result.all()]
            if not project_ids:
                raise ValidationError(
                    message="None of the specified project keys were found",
                    field="project_keys",
                )
    elif project_key is not None:
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
    )
    active_filters = filters if has_filters else None

    has_query = bool(q.strip())
    if not has_query and active_filters is None:
        raise ValidationError(
            message="Provide a search query or a filter",
            field="q",
        )

    type_counts: dict[str, int] = {}
    if not has_query:
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

    # Audit log the search query
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
