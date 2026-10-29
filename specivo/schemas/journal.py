"""Pydantic schemas for journals and journal details."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from specivo.schemas.common import IdName


class JournalDetailOut(BaseModel):
    """One field change within a journal entry.

    ``old_value`` / ``new_value`` hold what history stores: for a change naming
    a record (``status_id``, ``assigned_to_id``, ``fixed_version_id``, a
    ``cf_user`` / ``cf_version`` custom field, …) its id, which ``old_name`` /
    ``new_name`` name. A name is given only for a record the caller may see,
    by the issue page's rules; it is null for a plain value, and for an id the
    caller is not shown (show the value as stored). Imported history can hold
    the source's text instead of an id, for a record that was not imported.

    An ``attachment`` change (a file added or removed) has the attachment's
    id as ``prop_key`` and the file name as ``new_value`` (added) or
    ``old_value`` (removed); ``new_name`` / ``old_name`` give the file's name,
    its current one when the caller may open it, else the stored one.

    A custom field change (``cf``, ``cf_user``, ``cf_version``) has the
    field's metadata key as ``prop_key`` and its name as ``field_name``: the
    title its metadata schema gives it, else the key humanised. Null for a
    change of anything else.
    """

    model_config = {"from_attributes": True}

    id: int
    property: str
    prop_key: str
    old_value: str | None
    new_value: str | None
    old_name: str | None = None
    new_name: str | None = None
    field_name: str | None = None


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
