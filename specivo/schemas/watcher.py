"""Pydantic schemas for watchers."""

from __future__ import annotations

from pydantic import BaseModel


class WatcherOut(BaseModel):
    """A user watching an issue.

    No email address: the watcher list is readable by anyone who can see the
    issue, and a user's email is private.
    """

    model_config = {"from_attributes": True}

    id: int
    login: str
    display_name: str
