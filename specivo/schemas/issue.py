"""Pydantic schemas for Issue create/response."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator

from specivo.schemas.common import IdName, PaginatedResponse


class IssueCreate(BaseModel):
    """Payload for creating a new issue.

    ``project_key`` is resolved to a Project in the service layer.
    ``status_id`` and ``priority_id`` are optional — the service applies
    defaults (tracker's default_status / "Normal" priority) when omitted.
    ``parent_id`` is the internal DB id of the parent issue; when set the
    NestedSetService places the new issue as the rightmost child.
    """

    project_key: str
    tracker_id: int
    subject: str = Field(min_length=1, max_length=1024)
    description: str | None = None
    status_id: int | None = None
    priority_id: int | None = None
    assigned_to_id: int | None = None
    category_id: int | None = None
    parent_id: int | None = None
    start_date: date | None = None
    due_date: date | None = None
    estimated_hours: Decimal | None = Field(None, ge=0)
    done_ratio: int = Field(default=0, ge=0, le=100)
    fixed_version_id: int | None = None
    sprint_id: int | None = None
    is_private: bool = False
    metadata: dict = Field(default_factory=dict)

    @field_validator("subject")
    @classmethod
    def _strip_subject(cls, v: str) -> str:
        """Trim surrounding whitespace and reject blank subjects."""
        v = v.strip()
        if not v:
            raise ValueError("subject must not be empty or whitespace only")
        return v


class IssueUpdate(BaseModel):
    """Payload for partial update of an issue (PATCH semantics).

    All content fields are optional.  ``lock_version`` is REQUIRED for
    optimistic locking — the server rejects updates where the submitted
    version does not match the current DB value (409 Conflict).

    ``parent_id`` moves the issue in the hierarchy:
    - Set to an issue id to make it a child of that issue.
    - Set to ``0`` to move the issue to root (no parent).
    - Omit (``None``) to leave the parent unchanged.
    """

    tracker_id: int | None = None
    status_id: int | None = None
    priority_id: int | None = None
    subject: str | None = Field(None, min_length=1, max_length=1024)
    description: str | None = None
    assigned_to_id: int | None = None
    category_id: int | None = None
    parent_id: int | None = None  # None = no change; 0 = move to root
    start_date: date | None = None
    due_date: date | None = None
    estimated_hours: Decimal | None = Field(None, ge=0)
    done_ratio: int | None = Field(None, ge=0, le=100)
    fixed_version_id: int | None = None
    sprint_id: int | None = None
    is_private: bool | None = None
    metadata: dict | None = None
    lock_version: int  # REQUIRED — must match current DB value

    @field_validator("subject")
    @classmethod
    def _strip_subject(cls, v: str | None) -> str | None:
        """Trim surrounding whitespace and reject blank subjects.

        ``None`` is preserved (means "do not change"); an explicitly provided
        subject must contain at least one non-whitespace character.
        """
        if v is None:
            return None
        v = v.strip()
        if not v:
            raise ValueError("subject must not be empty or whitespace only")
        return v


class IssueMove(BaseModel):
    """Payload for moving an issue to another project."""

    target_project_key: str
    notes: str | None = None


class IssueFilters(BaseModel):
    """Query parameters for issue list filtering."""

    status: str | None = "open"  # "open", "closed", "all", or numeric status id
    tracker_id: int | None = None
    version_id: int | None = None
    assigned_to_id: str | None = None  # numeric id or "me"
    priority_id: int | None = None
    category_id: int | None = None
    author_id: int | None = None
    subject_contains: str | None = None
    created_after: date | None = None
    created_before: date | None = None
    updated_after: date | None = None
    updated_before: date | None = None


class IssueOut(BaseModel):
    """Issue representation returned by the API.

    ``key`` is the display key (e.g. ``"ACME-42"``).
    ``tracker``, ``status``, ``priority`` use ``IdName`` (id + name pairs)
    because they are non-navigable lookup values.
    ``author`` and ``assigned_to`` are also IdName for Phase 1; they will
    become full EntityRef in a later milestone.

    Hierarchy fields (nested set — Phase 1.5):
    - ``parent_id``: internal DB id of the direct parent (None for root issues)
    - ``root_id``: internal DB id of the tree root
    - ``lft``, ``rgt``: nested set boundary values
    """

    model_config = {"from_attributes": True}

    id: int
    key: str  # display_key (e.g. "ACME-42")
    project_key: str
    subject: str
    description: str | None
    tracker: IdName
    status: IdName
    priority: IdName
    author: IdName
    assigned_to: IdName | None
    category: IdName | None = None
    fixed_version_id: int | None = None
    fixed_version: IdName | None = None
    sprint_id: int | None = None
    parent_id: int | None = None
    root_id: int | None = None
    lft: int = 1
    rgt: int = 2
    done_ratio: int
    start_date: date | None
    due_date: date | None
    estimated_hours: Decimal | None
    metadata: dict
    is_private: bool
    lock_version: int
    created_at: datetime
    updated_at: datetime


class IssueListResponse(PaginatedResponse[IssueOut]):
    """Paginated issue list response."""

    pass


class IssueWithChildren(IssueOut):
    """IssueOut extended with optional included relations."""

    children: list[IssueOut] = Field(default_factory=list)
    journals: list | None = Field(default=None)
    watchers: list | None = Field(default=None)
    attachments: list | None = Field(default=None)
