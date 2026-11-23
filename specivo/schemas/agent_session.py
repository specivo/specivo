"""Pydantic schemas for AgentSession endpoints."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class AgentSessionOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    api_key_id: int | None
    user_id: int
    issue_id: int | None
    client_name: str | None
    client_version: str | None
    client_type: str | None
    external_session_id: str | None
    parent_external_session_id: str | None
    parent_session_id: int | None
    started_at: datetime
    last_activity_at: datetime
    ended_at: datetime | None
    created_at: datetime
    updated_at: datetime
