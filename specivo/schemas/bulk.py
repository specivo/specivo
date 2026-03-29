"""Pydantic schemas for bulk operations."""

from __future__ import annotations

from pydantic import BaseModel, Field


class BulkUpdateRequest(BaseModel):
    """Request payload for bulk-updating issues.

    ``issue_ids``: list of internal issue IDs to update (1..100).
    ``updates``: dict of fields to apply — same keys as IssueUpdate
    (status_id, assigned_to_id, fixed_version_id, priority_id, etc.)
    but without lock_version (handled internally).
    """

    issue_ids: list[int] = Field(min_length=1, max_length=100)
    updates: dict


class BulkDeleteRequest(BaseModel):
    """Request payload for bulk-deleting issues."""

    issue_ids: list[int] = Field(min_length=1, max_length=100)


class BulkResultItem(BaseModel):
    """Per-issue result in a bulk operation."""

    id: int
    key: str
    success: bool
    error: dict | None = None


class BulkResult(BaseModel):
    """Aggregate result of a bulk operation."""

    succeeded: list[BulkResultItem]
    failed: list[BulkResultItem]
