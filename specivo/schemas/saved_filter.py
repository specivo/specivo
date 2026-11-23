"""Pydantic schemas for saved filters."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, field_validator


class SavedFilterDefinition(BaseModel):
    """The values of a saved filter, typed like the issue list's query parameters.

    The list copies them into its SQL filters, so a value of the wrong type
    (``"tracker_id": "x"``) must be refused when the filter is saved, not fail
    every list that uses it. Keys the list does not know are kept as they are.
    """

    model_config = ConfigDict(extra="allow")

    status: str | int | None = None
    tracker_id: int | None = None
    assigned_to_id: int | str | None = None
    priority_id: int | None = None
    category_id: int | None = None
    version_id: int | str | None = None
    fixed_version_id: int | str | None = None
    tag_id: int | None = None
    author_id: int | None = None
    subject_contains: str | None = None
    created_after: date | datetime | None = None
    created_before: date | datetime | None = None
    updated_after: date | datetime | None = None
    updated_before: date | datetime | None = None
    sort: str | None = None


class SavedFilterCreate(BaseModel):
    """Create a new saved filter."""

    name: str
    filter_definition: dict
    is_public: bool = False

    @field_validator("filter_definition")
    @classmethod
    def _check_definition(cls, value: dict) -> dict:
        SavedFilterDefinition.model_validate(value)
        return value


class SavedFilterUpdate(BaseModel):
    """Partial update for a saved filter."""

    name: str | None = None
    filter_definition: dict | None = None
    is_public: bool | None = None

    @field_validator("filter_definition")
    @classmethod
    def _check_definition(cls, value: dict | None) -> dict | None:
        if value is not None:
            SavedFilterDefinition.model_validate(value)
        return value


class SavedFilterOut(BaseModel):
    """Saved filter response."""

    model_config = {"from_attributes": True}

    id: int
    name: str
    user_id: int
    project_id: int
    filter_definition: dict
    is_public: bool
    position: int
    created_at: datetime
    updated_at: datetime
