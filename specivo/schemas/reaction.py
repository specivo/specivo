"""Pydantic schemas for reactions and mentions."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class AddReactionRequest(BaseModel):
    """Request body for adding a reaction to a journal."""

    emoji: str = Field(min_length=1, max_length=50)


class ReactionOut(BaseModel):
    """Response for a single reaction."""

    model_config = {"from_attributes": True}

    id: int
    journal_id: int
    user_id: int
    emoji: str
    created_at: datetime


class ReactionUserOut(BaseModel):
    """A user who reacted with a given emoji."""

    id: int
    login: str
    display_name: str


class ReactionGroupOut(BaseModel):
    """Reactions grouped by emoji."""

    emoji: str
    count: int
    users: list[ReactionUserOut]


class UserAutocompleteOut(BaseModel):
    """User autocomplete result for mention UI."""

    model_config = {"from_attributes": True}

    id: int
    login: str
    display_name: str
    avatar_url: str | None
