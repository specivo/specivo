"""Search API — full-text search across issues and wiki pages (M2.3, M7.2, M7.3)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.constants import MAX_SEARCH_OFFSET
from specivo.core.database import DB_SESSION
from specivo.core.exceptions import (
    AnonymousAccessDeniedError,
    NotFoundError,
    PermissionDeniedError,
    UnauthorizedError,
    ValidationError,
)
from specivo.core.search_limits import enforce_search_rate_limit
from specivo.core.security import (
    ANONYMOUS_SEARCH_MAX_LIMIT,
    ANONYMOUS_SEARCH_MAX_OFFSET,
    get_reader,
)
from specivo.models.project import Project
from specivo.models.user import User
from specivo.schemas.links import LinkGraphOut
from specivo.schemas.search import SearchFilters, SearchResponse, SearchResult
from specivo.services.entity_link_query import expand_hits
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

# How far a visitor without an account may page. Defined in
# specivo.core.security, which the web search page reads too, so the API's
# bounds and the page's cannot drift apart.
_ANONYMOUS_SEARCH_MAX_LIMIT = ANONYMOUS_SEARCH_MAX_LIMIT
_ANONYMOUS_SEARCH_MAX_OFFSET = ANONYMOUS_SEARCH_MAX_OFFSET


async def _search_budget(
    request: Request,
    response: Response,
    user: User = Depends(get_reader, scope="function"),
) -> None:
    """Spend one search from the caller's per-IP budget (specivo.core.search_limits).

    A dependency of its own rather than a ``rate_limit`` on the route, because
    which bucket applies is only known once ``get_reader`` has resolved the
    caller: a visitor without an account spends ``anon_search``, anyone signed
    in spends ``search``. ``get_reader`` is the same callable the route
    depends on, so FastAPI resolves it once and both see one principal.
    """
    await enforce_search_rate_limit(request, response, anonymous=user.is_anonymous)


async def _project_or_none(db: AsyncSession, key: str) -> Project | None:
    """Project *key* (any case, or a retired key) within the API key's limit, or ``None``."""
    try:
        return await _project_service.get_by_key(db, key.upper())
    except NotFoundError:
        return None


async def _can_reach(db: AsyncSession, project: Project, user: User) -> bool:
    """Whether a signed-in *user* may reach *project* (membership, public flag, key limit).

    Every refusal ``require_project_access`` can give (not found, forbidden,
    unauthorized) is folded into False, so the caller answers each one the
    way it answers a key that names no project.
    """
    try:
        await _project_service.require_project_access(db, project, user)
    except (NotFoundError, PermissionDeniedError, UnauthorizedError):
        return False
    return True


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


async def _resolve_project_filter(
    db: AsyncSession, user: User, project_key: str | None, project_keys: str | None
) -> tuple[int | None, list[int] | None]:
    """The ``(project_id, project_ids)`` the ``project_key`` / ``project_keys`` filter names.

    ``project_keys`` wins when both are given. A project the caller may not
    reach is refused the same way a key naming no project is.
    """
    if project_keys is not None:
        return None, await _resolve_project_keys(db, user, project_keys)
    if project_key is None:
        return None, None
    if user.is_anonymous:
        return (await _project_service.get_readable_by_key(db, project_key.upper(), user)).id, None
    target = await _project_or_none(db, project_key)
    # A project the caller may not reach gets the same answer as one
    # that does not exist, so the filter cannot probe for private keys.
    if target is None or not await _can_reach(db, target, user):
        raise ValidationError(
            message=f"Project with key '{project_key}' not found",
            field="project_key",
        )
    return target.id, None


async def _resolve_project_keys(db: AsyncSession, user: User, project_keys: str) -> list[int] | None:
    """The ids of the comma-separated *project_keys* (multi-project search); ``None`` when the list is empty."""
    keys = [k.strip() for k in project_keys.split(",") if k.strip()]
    if not keys:
        return None
    if user.is_anonymous:
        # Resolved one at a time through the reader's own project lookup,
        # so a key naming no project and a key naming one this visitor may
        # not read are refused identically. The bulk query below cannot do
        # that: it answers "no results" for the unreadable project and an
        # error for the missing one, and the difference is the disclosure.
        return [(await _project_service.get_readable_by_key(db, key.upper(), user)).id for key in keys]
    # Each key the way every other project lookup reads it: any case,
    # a retired key, and outside the API key's limit as missing.
    projects = [p for p in [await _project_or_none(db, key) for key in keys] if p is not None]
    # Keep only projects the caller may reach; a private project they
    # are not a member of counts as missing, never as "no results".
    project_ids = [p.id for p in projects if await _can_reach(db, p, user)]
    if not project_ids:
        raise ValidationError(
            message="None of the specified project keys were found",
            field="project_keys",
        )
    return project_ids


def _parse_metadata_filter(metadata: str | None) -> dict | None:
    """The ``metadata`` containment filter as a dict, ``None`` when blank; size, JSON and key count are checked."""
    if metadata is None or not metadata.strip():
        return None
    # Cap the raw payload size before parsing to bound resource use.
    if len(metadata) > _METADATA_FILTER_MAX_BYTES:
        raise ValidationError(message="Metadata filter is too large", field="metadata")
    try:
        loaded = json.loads(metadata)
    except json.JSONDecodeError:
        raise ValidationError(  # noqa: B904
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
    return loaded


@dataclass(frozen=True, slots=True)
class _SearchRequest:
    """The resolved arguments of one search."""

    query: str
    mode: str
    scope: str
    project_id: int | None
    project_ids: list[int] | None
    offset: int
    limit: int
    filters: SearchFilters | None
    #: Whether a tag filter is given (a tag-only listing when there is no query).
    tagged: bool


async def _run_search(
    db: AsyncSession, user: User, req: _SearchRequest
) -> tuple[list[SearchResult], int, dict[str, int]]:
    """Run *req* in the service method its query and mode call for: ``(items, total, counts per type)``.

    Without a query it is a filter listing (tagged items, else issues);
    with one, the mode picks keyword, semantic or hybrid search. Semantic
    search reports no counts per type.
    """
    common: dict[str, Any] = {
        "session": db,
        "user": user,
        "project_id": req.project_id,
        "project_ids": req.project_ids,
        "offset": req.offset,
        "limit": req.limit,
        "filters": req.filters,
    }
    if not req.query.strip():
        if req.tagged:
            # Tag-only listing — issues + wiki, ordered by recency.
            return await _service.filter_tagged(**common, scope=req.scope)
        # Metadata/attribute-only listing — no full-text term.
        return await _service.filter_issues(**common)
    if req.mode == "semantic":
        items, total_count = await _service.semantic_search(**common, query=req.query, scope=req.scope)
        return items, total_count, {}
    run = _service.hybrid_search if req.mode == "hybrid" else _service.search
    return await run(**common, query=req.query, scope=req.scope)


async def _audit_search(
    db: AsyncSession,
    request: Request,
    user: User,
    *,
    query: str,
    mode: str,
    scope: str,
    filters: SearchFilters | None,
    result_count: int,
    type_counts: dict[str, int],
) -> None:
    """Record a signed-in caller's search in the audit log; a failure is logged and never fails the search."""
    try:
        filter_details: dict | None = None
        if filters is not None:
            filter_details = {k: v for k, v in filters.model_dump().items() if v is not None}
            # Convert datetime to string for JSON serialization
            for fk, fv in filter_details.items():
                if isinstance(fv, datetime):
                    filter_details[fk] = fv.isoformat()
        await _audit_service.log_search_query(
            session=db,
            user_id=user.id,
            query=query,
            mode=mode,
            scope=scope,
            filters=filter_details,
            result_count=result_count,
            type_counts=type_counts if mode != "semantic" else None,
            request=request,
        )
    except Exception:
        logger.warning("Failed to log search query audit", exc_info=True)


@router.get(
    "/search/",
    response_model=SearchResponse,
    responses={429: {"description": "rate_limit_exceeded: the per-IP search budget is spent; see Retry-After"}},
)
async def search(  # noqa: PLR0913
    request: Request,
    q: str = Query("", description="Search query (optional when a metadata filter is given)"),
    project_key: str | None = Query(None, description="Scope to a specific project"),
    project_keys: str | None = Query(None, description="Comma-separated project keys for multi-project search"),
    scope: str = Query("all", pattern="^(all|issues|wiki|comments|attachments)$", description="Search scope"),
    mode: str = Query("keyword", pattern="^(keyword|semantic|hybrid)$", description="Search mode"),
    offset: int = Query(0, ge=0, le=MAX_SEARCH_OFFSET, description="Pagination offset"),
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
    tag: list[str] = Query(default=[], description="Real-tag name(s) to filter by (AND logic)"),
    expand: int = Query(
        0,
        ge=0,
        le=1,
        description=(
            "1 to add `links` to the top 5 issue and wiki page hits: their neighbours in the link graph, "
            "one hop, at most 10 each, only what you may see"
        ),
    ),
    user: User = Depends(get_reader, scope="function"),
    _budget: None = Depends(_search_budget),
    db: AsyncSession = DB_SESSION,
) -> SearchResponse:
    """Search across issues and wiki pages.

    Supports three modes:
    - keyword: Full-text search using PostgreSQL tsvector (default)
    - semantic: Vector similarity search using pgvector embeddings
    - hybrid: RRF fusion of keyword + semantic results

    Results are sorted by relevance score (descending).
    With ``expand=1`` the top 5 issue and wiki page hits also carry ``links``:
    what each is linked to and from in the link graph, one hop (the shape of
    ``GET /issues/{ref}/links/``, at most 10 neighbours, tags, versions and
    other structured nodes as leaves), filtered for the caller.
    Access control enforces per-project visibility rules.

    Every caller is metered per client address, in every mode (429 with
    ``Retry-After`` over the limit). A visitor without an account may use
    every mode too, over a short page, and only ever sees what the anonymous
    visibility rules admit; a project filter that names a project they may not
    read is refused the same way one naming no project at all is.
    """
    anonymous = user.is_anonymous
    if anonymous and (limit > _ANONYMOUS_SEARCH_MAX_LIMIT or offset > _ANONYMOUS_SEARCH_MAX_OFFSET):
        raise AnonymousAccessDeniedError()

    project_id, project_ids = await _resolve_project_filter(db, user, project_key, project_keys)
    parsed_metadata = _parse_metadata_filter(metadata)
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

    # Tags attach only to issues and wiki pages; coerce non-taggable scopes.
    if tag_names and scope not in ("issues", "wiki"):
        scope = "all"

    if not q.strip() and not has_filters:
        raise ValidationError(
            message="Provide a search query or a filter",
            field="q",
        )

    items, total_count, type_counts = await _run_search(
        db,
        user,
        _SearchRequest(
            query=q,
            mode=mode,
            scope=scope,
            project_id=project_id,
            project_ids=project_ids,
            offset=offset,
            limit=limit,
            filters=filters if has_filters else None,
            tagged=bool(tag_names),
        ),
    )

    # Audit log the search query. Skipped for anonymous visitors, and this is
    # the guard that matters most: unlike ``log_event``, ``log_search_query``
    # writes unconditionally, so
    # without this an anonymous crawl would persist one row per request. It is
    # also the only write on this path, which a read-only transaction would
    # otherwise refuse outright.
    if not anonymous:
        await _audit_search(
            db,
            request,
            user,
            query=q,
            mode=mode,
            scope=scope,
            filters=filters if has_filters else None,
            result_count=total_count,
            type_counts=type_counts,
        )

    if expand:
        for index, graph in (await expand_hits(db, user, items)).items():
            items[index].links = LinkGraphOut.model_validate(graph.as_dict())

    return SearchResponse(
        total_count=total_count,
        offset=offset,
        limit=limit,
        items=items,
    )
