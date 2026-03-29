"""Pydantic schemas for Version resources."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

_STATUS = Literal["open", "locked", "closed"]
_SHARING = Literal["none", "descendants", "hierarchy", "tree", "system"]


class VersionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    status: _STATUS = "open"
    effective_date: date | None = None
    sharing: _SHARING = "none"
    wiki_page_title: str | None = None


class VersionUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    status: _STATUS | None = None
    effective_date: date | None = None
    sharing: _SHARING | None = None
    wiki_page_title: str | None = None


class VersionOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    name: str
    description: str | None
    status: str
    effective_date: date | None
    sharing: str
    wiki_page_title: str | None
    project_key: str
    created_at: datetime


class RoadmapEntry(BaseModel):
    version: VersionOut
    open_count: int
    closed_count: int
    total: int
    progress_percent: int
