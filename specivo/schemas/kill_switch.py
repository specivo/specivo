"""Pydantic schemas for kill switch API."""

from datetime import datetime

from pydantic import BaseModel


class KillRequest(BaseModel):
    reason: str


class KillEventOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    level: str
    target_type: str | None
    target_id: int | None
    triggered_by: str
    trigger_reason: str
    snapshot: dict
    created_at: datetime
