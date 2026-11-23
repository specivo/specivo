"""Web search page: unified search across issues and wiki."""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable
from typing import TYPE_CHECKING, cast

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.constants import MAX_SEARCH_OFFSET
from specivo.core.database import DB_SESSION
from specivo.core.search_limits import enforce_search_rate_limit
from specivo.core.security import ANONYMOUS_SEARCH_MAX_LIMIT, ANONYMOUS_SEARCH_MAX_OFFSET
from specivo.schemas.search import SearchFilters
from specivo.services.project_service import ProjectService
from specivo.services.search_service import SearchService
from specivo.services.security_audit_service import SecurityAuditService
from specivo.web.deps import get_templates, get_web_reader, refuse_anonymous_web

# Metadata filter slug charset and value length cap (defense against crafted input).
_MF_SLUG_RE = re.compile(r"^[a-z0-9_-]{1,64}$")
_MV_MAX_LEN = 255

# Real-tag filter caps. Tag names are at most 64 chars (Tag.name column), and we
# cap the number of simultaneously-applied tags to keep the AND query bounded.
_TAG_VALUE_MAX = 64
_TAG_MAX = 20

if TYPE_CHECKING:
    from specivo.models.user import User

router = APIRouter(tags=["web-search"], include_in_schema=False)

_search_svc = SearchService()
_project_svc = ProjectService()
_audit_svc = SecurityAuditService()


@router.get("/search/", response_class=HTMLResponse)
async def search_page(  # noqa: PLR0913
    request: Request,
    response: Response,
    reader: object | None = Depends(get_web_reader, scope="function"),
    db: AsyncSession = DB_SESSION,
    q: str = Query(""),
    mode: str = Query("hybrid"),
    scope: str = Query("all"),
    project_key: str = Query(""),
    mf: str = Query("", description="Metadata field slug to filter by"),
    mv: str = Query("", description="Metadata value to filter by"),
    ma: int = Query(0, description="1 if the metadata field is array-typed"),
    tag: list[str] = Query(default=[], description="Tag name(s) to filter by (AND logic)"),
    offset: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=100),
) -> Response:
    """Render the search page with results."""
    # Deep paging is clamped rather than refused, as for anonymous visitors
    # below: a hand-edited URL gets the last page search serves, not an error.
    offset = min(offset, MAX_SEARCH_OFFSET)
    if reader is None:
        return refuse_anonymous_web(request)
    user = cast("User", reader)
    anonymous = user.is_anonymous

    # Every search is metered per client address, in every mode and for
    # everyone (specivo.core.search_limits); a page that only shows the empty
    # form costs nothing and is not counted. Over the limit this raises 429,
    # which renders as a page rather than the API's JSON error.
    if q.strip() or (mf.strip() and mv.strip()) or any(t.strip() for t in tag):
        await enforce_search_rate_limit(request, response, anonymous=anonymous)

    if anonymous:
        # The same bounds the JSON API enforces, reached by narrowing the
        # request instead of refusing it: a browser arrives with whatever the
        # form put in the URL. The mode is left as asked, hybrid by default.
        limit = min(limit, ANONYMOUS_SEARCH_MAX_LIMIT)
        offset = min(offset, ANONYMOUS_SEARCH_MAX_OFFSET)

    # Resolve project_key to project_id if provided
    project_id = None
    if project_key:
        try:
            project_id = await _project_id_for(db, user, project_key)
        except _AnonymousRefusedError:
            return refuse_anonymous_web(request)

    # An optional metadata containment filter from mf/mv/ma; invalid or
    # incomplete pairs are ignored (treated as "no filter").
    active_filters, mf_clean, mv_clean = _metadata_filter(mf, mv, ma)

    # The filter reads by the field's name, and a person or version it holds by theirs.
    mf_label, mv_label = await _metadata_filter_labels(db, user, project_id, mf_clean, mv_clean)

    tag_clean = _clean_tag_names(tag)
    if tag_clean:
        # Tags only attach to issues and wiki pages, so coerce non-taggable
        # scopes to "all" (which the service narrows to issues + wiki).
        if scope not in ("issues", "wiki"):
            scope = "all"
        if active_filters is None:
            active_filters = SearchFilters(tag_names=tag_clean)
        else:
            active_filters = active_filters.model_copy(update={"tag_names": tag_clean})

    if q.strip():
        results, total, type_counts = await _keyword_or_hybrid(
            db, user, q, mode, scope, project_id, offset, limit, active_filters
        )
    elif active_filters is not None and tag_clean:
        results, total, type_counts = await _guarded(
            _search_svc.filter_tagged(
                db, user=user, project_id=project_id, scope=scope, offset=offset, limit=limit, filters=active_filters
            ),
            "Tag filter failed for tags=%r",
            tag_clean,
        )
    elif active_filters is not None:
        # Metadata-only listing: force the issues scope (only issues carry metadata).
        scope = "issues"
        results, total, type_counts = await _guarded(
            _search_svc.filter_issues(
                db, user=user, project_id=project_id, offset=offset, limit=limit, filters=active_filters
            ),
            "Metadata filter failed for mf=%r mv=%r",
            mf_clean,
            mv_clean,
        )
    else:
        results, total, type_counts = [], 0, {}

    # Audit log the search/filter query. Never for an anonymous visitor: the
    # log exists to attribute a search to somebody, a crawler would add a row
    # per request under a single principal, and it is the only write on this
    # path — which the read-only transaction the request runs in would refuse
    # outright.
    if not anonymous and (q.strip() or active_filters is not None):
        await _audit_search(db, request, user, q, mode, scope, total, type_counts)

    templates = get_templates()
    return templates.TemplateResponse(
        request,
        "pages/search.html",
        context={
            "user": user,
            "anonymous": anonymous,
            "active_page": "search",
            "query": q,
            "mode": mode,
            "scope": scope,
            "project_key": project_key,
            "mf": mf_clean,
            "mv": mv_clean,
            "mf_label": mf_label,
            "mv_label": mv_label,
            "ma": ma,
            "tags": [{"name": n} for n in tag_clean],
            "results": results,
            "total": total,
            "offset": offset,
            "limit": limit,
            "type_counts": type_counts,
        },
    )


async def _metadata_filter_labels(
    db: AsyncSession, user: User, project_id: int | None, key: str, value: str
) -> tuple[str, str]:
    """The name of the metadata field *key* filters by, and of the person or version *value* names.

    From the issue metadata schemas of the projects searched (*project_id*, or
    every project *user* may open). A value of a user or version field is
    named when *user* may see what it names; anything else reads as given.
    """
    if not key:
        return key, value
    from sqlalchemy import select

    from specivo.models.metadata_schema import MetadataSchema
    from specivo.models.project import Project
    from specivo.services.activity_lookups import metadata_value_names
    from specivo.services.metadata_fields import field_labels, humanise

    stmt = (
        select(MetadataSchema)
        .join(Project, Project.id == MetadataSchema.project_id)
        .where(MetadataSchema.content_type == "issue")
    )
    if project_id is not None:
        stmt = stmt.where(MetadataSchema.project_id == project_id)
    readable = await _project_svc.accessible_projects_clause(db, user)
    if readable is not None:
        stmt = stmt.where(readable)
    schemas = list((await db.execute(stmt)).scalars().all())
    label = field_labels(schemas).get(key) or humanise(key)
    named = await metadata_value_names(db, user, schemas, {key: int(value) if value.isdigit() else value})
    return label, named.get(key, {}).get(value, value)


class _AnonymousRefusedError(Exception):
    """The anonymous visitor may not read the project the search is scoped to."""


async def _project_id_for(db: AsyncSession, user: User, project_key: str) -> int | None:
    """The id of the project *project_key* scopes the search to; ``None`` searches globally.

    A visitor without an account is refused (:class:`_AnonymousRefusedError`)
    for a key naming no project and one naming a project they may not read
    alike. A signed-in user's unknown key falls back to a global search.
    """
    from specivo.core.exceptions import AnonymousAccessDeniedError, NotFoundError

    if user.is_anonymous:
        # Resolved through the reader's own project lookup, so a key naming
        # no project and one naming a project this visitor may not read are
        # refused identically. Falling back to a global search — what the
        # signed-in path does — would answer the two differently, and the
        # difference is the disclosure.
        try:
            return (await _project_svc.get_readable_by_key(db, project_key, user)).id
        except AnonymousAccessDeniedError:
            raise _AnonymousRefusedError from None
    try:
        project = await _project_svc.get_by_key(db, project_key)
        await _project_svc.require_project_access(db, project, user)
    except NotFoundError:
        return None  # Ignore invalid project key, search globally
    return project.id


def _metadata_filter(mf: str, mv: str, ma: int) -> tuple[SearchFilters | None, str, str]:
    """The containment filter *mf* / *mv* / *ma* ask for, with the cleaned field and value.

    An invalid or incomplete pair gives no filter and blank field and value,
    so the template does not show a stale chip.
    """
    mf_clean = mf.strip()
    mv_clean = mv.strip()
    if mf_clean and mv_clean and _MF_SLUG_RE.match(mf_clean) and len(mv_clean) <= _MV_MAX_LEN:
        containment = {mf_clean: [mv_clean]} if ma else {mf_clean: mv_clean}
        return SearchFilters(metadata=containment), mf_clean, mv_clean
    return None, "", ""


def _clean_tag_names(tag: list[str]) -> list[str]:
    """The real-tag filter from repeated ``tag=`` params (a comma-separated value is split too).

    Names are stripped, length-capped, deduplicated case-insensitively, and
    clamped to a sane maximum.
    """
    tag_clean: list[str] = []
    seen: set[str] = set()
    for raw in tag:
        for part in raw.split(",") if "," in raw else [raw]:
            name = part.strip()
            if not name or len(name) > _TAG_VALUE_MAX or name.lower() in seen:
                continue
            seen.add(name.lower())
            tag_clean.append(name)
            if len(tag_clean) >= _TAG_MAX:
                return tag_clean
    return tag_clean


_SearchOutcome = tuple[list, int, dict[str, int]]


async def _guarded(run: Awaitable[_SearchOutcome], message: str, *args: object) -> _SearchOutcome:
    """Await a search; a failure is logged with *message* and shows as no results."""
    try:
        return await run
    except Exception:
        logging.getLogger(__name__).exception(message, *args)
        return [], 0, {}


async def _keyword_or_hybrid(
    db: AsyncSession,
    user: User,
    q: str,
    mode: str,
    scope: str,
    project_id: int | None,
    offset: int,
    limit: int,
    filters: SearchFilters | None,
) -> _SearchOutcome:
    """A full-text search: hybrid when *mode* asks for it, else keyword."""
    search_fn = _search_svc.hybrid_search if mode == "hybrid" else _search_svc.search
    return await _guarded(
        search_fn(db, q, user=user, project_id=project_id, scope=scope, offset=offset, limit=limit, filters=filters),
        "Search failed for q=%r scope=%s mode=%s",
        q,
        scope,
        mode,
    )


async def _audit_search(
    db: AsyncSession,
    request: Request,
    user: User,
    q: str,
    mode: str,
    scope: str,
    total: int,
    type_counts: dict[str, int],
) -> None:
    """Record a signed-in user's search in the audit log; a failure never breaks the page."""
    try:  # noqa: SIM105
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
    except Exception:  # noqa: BLE001, S110
        pass
