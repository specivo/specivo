"""Pydantic schemas for Tag resources."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

# Hex color like "#4f9d6c" (3- or 6-digit). Optional.
_COLOR_PATTERN = r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$"


class TagCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    color: str | None = Field(default=None, pattern=_COLOR_PATTERN)


class TagUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    color: str | None = Field(default=None, pattern=_COLOR_PATTERN)


class TagOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    name: str
    color: str | None
    project_key: str
    created_at: datetime


class TagWithUsageOut(TagOut):
    issue_count: int = 0
    wiki_count: int = 0


class EntityTagsSet(BaseModel):
    """Replace the full tag set on an entity (names; created on the fly)."""

    names: list[str] = Field(default_factory=list)


class EntityTagAdd(BaseModel):
    """Add a single tag to an entity (created on the fly if new)."""

    name: str = Field(min_length=1, max_length=64)


class BulkTagRequest(BaseModel):
    """Apply/remove tags across many issues at once."""

    issue_ids: list[int] = Field(default_factory=list)
    add: list[str] = Field(default_factory=list)
    remove: list[int] = Field(default_factory=list)
