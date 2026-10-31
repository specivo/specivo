"""Pydantic schemas for Wiki pages, content, and versioning."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from specivo.schemas.common import IdName  # noqa: TC001

if TYPE_CHECKING:
    from specivo.models.user import User
    from specivo.models.wiki import WikiContent, WikiPage

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
    title: str | None = Field(None, min_length=1, max_length=255)
    parent_slug: str | None = None
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


def page_with_content(page: WikiPage, content: WikiContent, author: User) -> WikiPageWithContent:
    """Build a WikiPageWithContent from a page, one of its versions and that version's author."""
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
        content_author=IdName(id=content.author_id, name=author.display_name),
        content_updated_at=content.created_at,
    )


class WikiContentVersionOut(BaseModel):
    model_config = {"from_attributes": True}

    version: int
    author: IdName
    comments: str | None
    created_at: datetime
    text: str


class WikiDeletedPageOut(WikiPageOut):
    deleted_at: datetime | None = None
    deleted_by_id: int | None = None


class WikiPageListResponse(BaseModel):
    items: list[WikiPageOut]


class WikiTrashListResponse(BaseModel):
    items: list[WikiDeletedPageOut]


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
