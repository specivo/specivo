"""Pydantic schemas for Tag resources."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints, field_validator

# Hex color like "#4f9d6c" (3- or 6-digit). Optional.
_COLOR_PATTERN = r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$"

# Bounds for tag lists: the per-name length matches the ``tags.name`` column,
# and the list caps stop one request from fanning out into thousands of
# get-or-create round trips and link writes.
TAG_NAME_MAX = 64
MAX_TAG_NAMES = 100
MAX_BULK_ISSUE_IDS = 1000

# One tag name in a list: stripped first, so surrounding blanks do not count
# against the length. Pydantic's own length message is what the caller sees,
# the same one a single over-long name gets from ``TagCreate``.
TagNameItem = Annotated[str, StringConstraints(strip_whitespace=True, max_length=TAG_NAME_MAX)]


def normalize_tag_names(values: list[str]) -> list[str]:
    """Drop blank names and case-insensitive repeats, keeping first-seen order."""
    cleaned: list[str] = []
    seen: set[str] = set()
    for name in values:
        key = name.lower()
        if not name or key in seen:
            continue
        seen.add(key)
        cleaned.append(name)
    return cleaned


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

    names: list[TagNameItem] = Field(default_factory=list, max_length=MAX_TAG_NAMES)

    @field_validator("names")
    @classmethod
    def _check_names(cls, v: list[str]) -> list[str]:
        return normalize_tag_names(v)


class EntityTagAdd(BaseModel):
    """Add a single tag to an entity (created on the fly if new)."""

    name: str = Field(min_length=1, max_length=TAG_NAME_MAX)


class BulkTagRequest(BaseModel):
    """Apply/remove tags across many issues at once."""

    issue_ids: list[int] = Field(default_factory=list, max_length=MAX_BULK_ISSUE_IDS)
    add: list[TagNameItem] = Field(default_factory=list, max_length=MAX_TAG_NAMES)
    remove: list[int] = Field(default_factory=list, max_length=MAX_TAG_NAMES)

    @field_validator("add")
    @classmethod
    def _check_add(cls, v: list[str]) -> list[str]:
        return normalize_tag_names(v)

    @field_validator("issue_ids", "remove")
    @classmethod
    def _dedupe_ids(cls, v: list[int]) -> list[int]:
        # A repeated issue id would otherwise queue the same link twice.
        return list(dict.fromkeys(v))
