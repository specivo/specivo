"""Pydantic schemas for full-text search (M2.3, M7.3)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel

from specivo.schemas.common import PaginatedResponse


class SearchResult(BaseModel):
    """A single search result from issues, wiki pages, or comments."""

    result_type: str  # "issue" | "wiki" | "comment"
    id: int
    title: str
    subtitle: str | None = None
    snippet: str | None = None
    score: float
    project_key: str


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
