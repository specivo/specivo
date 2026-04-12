"""Pydantic schemas for Sprint resources."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class SprintCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    goal: str | None = None
    start_date: date | None = None
    end_date: date | None = None


class SprintUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    goal: str | None = None
    start_date: date | None = None
    end_date: date | None = None


class SprintOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    project_id: int
    name: str
    goal: str | None
    status: str
    start_date: date | None
    end_date: date | None
    velocity_snapshot: dict | None = None
    created_at: datetime
    updated_at: datetime


class SprintCompleteRequest(BaseModel):
    move_incomplete_to_sprint_id: int | None = None


class SprintListResponse(BaseModel):
    items: list[SprintOut]


class BurndownDataPoint(BaseModel):
    date: date
    remaining_hours: Decimal
    ideal_remaining: Decimal


class BurndownOut(BaseModel):
    total_estimated_hours: Decimal
    completed_hours: Decimal
    data_points: list[BurndownDataPoint]
