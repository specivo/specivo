"""Pydantic schemas for saved filters."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class SavedFilterCreate(BaseModel):
    """Create a new saved filter."""

    name: str
    filter_definition: dict
    is_public: bool = False


class SavedFilterUpdate(BaseModel):
    """Partial update for a saved filter."""

    name: str | None = None
    filter_definition: dict | None = None
    is_public: bool | None = None


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
