"""Pydantic schemas for watchers."""

from __future__ import annotations

from pydantic import BaseModel


class WatcherOut(BaseModel):
    """A user watching an issue."""

    model_config = {"from_attributes": True}

    id: int
    login: str
    display_name: str
    email: str
