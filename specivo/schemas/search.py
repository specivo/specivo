"""Pydantic schemas for full-text search (M2.3, M7.3)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from specivo.schemas.common import PaginatedResponse


class SearchResultType(StrEnum):
    """Canonical display types for search results.

    These are the types used in RRF keys, templates, and API responses.
    Storage types in ``search_sources.source_type`` may differ (e.g.
    ``wiki_page`` in DB → ``wiki`` for display). See :class:`SearchSourceType`
    for the storage-layer values.
    """

    ISSUE = "issue"
    WIKI = "wiki"
    COMMENT = "comment"
    ATTACHMENT = "attachment"


class SearchSourceType(StrEnum):
    """Storage-layer source_type values as written to ``search_sources.source_type``.

    These are the raw DB values. They differ from :class:`SearchResultType`
    for wiki (``wiki_page`` vs ``wiki``) and comments (``journal`` vs
    ``comment``). Use this enum wherever raw SQL references ``source_type``
    column values or where ``EmbeddingService`` / backfill code writes to
    ``SearchSource.source_type``.
    """

    ISSUE = "issue"
    WIKI_PAGE = "wiki_page"
    JOURNAL = "journal"
    ATTACHMENT = "attachment"


#: Map DB source_type values to canonical display types.
SOURCE_TYPE_TO_DISPLAY: dict[str, SearchResultType] = {
    SearchSourceType.ISSUE.value: SearchResultType.ISSUE,
    SearchSourceType.WIKI_PAGE.value: SearchResultType.WIKI,
    "wiki": SearchResultType.WIKI,
    SearchSourceType.JOURNAL.value: SearchResultType.COMMENT,
    "comment": SearchResultType.COMMENT,
    SearchSourceType.ATTACHMENT.value: SearchResultType.ATTACHMENT,
}


class SearchResult(BaseModel):
    """A single search result from issues, wiki pages, or comments."""

    result_type: str
    id: int
    title: str
    subtitle: str | None = None
    snippet: str | None = None
    score: float
    project_key: str
    fts_rank: int | None = None
    sem_rank: int | None = None


class SearchResponse(PaginatedResponse[SearchResult]):
    """Paginated search results."""


class SearchFilters(BaseModel):
    """Metadata filters for issue search results."""

    tracker_id: int | None = None
    status_id: int | None = None
    priority_id: int | None = None
    assigned_to_id: int | None = None
    author_id: int | None = None
    category_id: int | None = None
    fixed_version_id: int | None = None
    created_after: datetime | None = None
    created_before: datetime | None = None
    updated_after: datetime | None = None
    updated_before: datetime | None = None
    metadata: dict[str, Any] | None = None
    #: Real-tag names to filter by (case-insensitive). Multiple names combine
    #: with AND logic (the entity must carry every named tag). Tags are matched
    #: by name, not id, so the filter spans projects.
    tag_names: list[str] | None = None
