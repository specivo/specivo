"""Pydantic schemas for Wiki pages, content, and versioning."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from specivo.schemas.common import IdName  # noqa: TC001

# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class WikiPageCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    text: str = ""
    parent_slug: str | None = None
    comments: str | None = None  # edit summary


class WikiPageUpdate(BaseModel):
    text: str
    lock_version: int  # required for optimistic locking
    comments: str | None = None


class WikiPageRename(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    lock_version: int


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class WikiPageOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    title: str
    slug: str
    parent_id: int | None = None
    protected: bool
    lock_version: int
    created_at: datetime
    updated_at: datetime


class WikiPageWithContent(WikiPageOut):
    text: str
    content_version: int
    content_author: IdName
    content_updated_at: datetime


class WikiContentVersionOut(BaseModel):
    model_config = {"from_attributes": True}

    version: int
    author: IdName
    comments: str | None
    created_at: datetime
    text: str


class WikiPageListResponse(BaseModel):
    items: list[WikiPageOut]


class WikiVersionsResponse(BaseModel):
    versions: list[WikiContentVersionOut]


# ---------------------------------------------------------------------------
# Link graph schemas
# ---------------------------------------------------------------------------


class WikiGraphNode(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    slug: str
    title: str


class WikiGraphEdge(BaseModel):
    model_config = {"from_attributes": True}

    source_page_id: int
    target_page_id: int | None = None
    target_slug: str
    display_text: str | None = None
    is_broken: bool


class WikiGraphResponse(BaseModel):
    nodes: list[WikiGraphNode]
    edges: list[WikiGraphEdge]
