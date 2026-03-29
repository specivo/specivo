"""Pydantic schemas for AgentSession endpoints."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class AgentSessionOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    api_key_id: int
    user_id: int
    issue_id: int | None
    model_name: str | None
    started_at: datetime
    last_activity_at: datetime
    created_at: datetime
    updated_at: datetime
