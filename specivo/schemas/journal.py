"""Pydantic schemas for journals and journal details."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from specivo.schemas.common import IdName


class JournalDetailOut(BaseModel):
    """One field change within a journal entry."""

    model_config = {"from_attributes": True}

    id: int
    property: str
    prop_key: str
    old_value: str | None
    new_value: str | None


class JournalOut(BaseModel):
    """A single activity stream entry (field change and/or comment)."""

    model_config = {"from_attributes": True}

    id: int
    sequence: int
    issue_id: int | None
    user: IdName
    notes: str | None
    is_private: bool
    details: list[JournalDetailOut] = Field(default_factory=list)
    reply_to_id: int | None = None
    is_resolved: bool = False
    resolved_by: IdName | None = None
    resolved_at: datetime | None = None
    resolved_summary: str | None = None
    created_at: datetime
    updated_at: datetime


class AddCommentRequest(BaseModel):
    """Request body for adding a comment to an issue."""

    notes: str = Field(min_length=1)
    reply_to_id: int | None = None


class ResolveThreadRequest(BaseModel):
    """Request body for resolving a journal thread."""

    summary: str = Field(min_length=1)
