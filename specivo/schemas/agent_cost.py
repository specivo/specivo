"""Pydantic schemas for agent cost tracking endpoints."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# ModelCostConfig
# ---------------------------------------------------------------------------


class ModelCostConfigCreate(BaseModel):
    provider: str = Field(max_length=50)
    model_name: str = Field(max_length=100)
    input_cost_per_1m: Decimal = Field(ge=0, decimal_places=4, max_digits=10)
    output_cost_per_1m: Decimal = Field(ge=0, decimal_places=4, max_digits=10)


class ModelCostConfigOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    provider: str
    model_name: str
    input_cost_per_1m: Decimal
    output_cost_per_1m: Decimal
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# AgentTokenLog
# ---------------------------------------------------------------------------


class AgentTokenLogCreate(BaseModel):
    session_id: int
    model_name: str = Field(max_length=100)
    input_tokens: int = Field(ge=0, default=0)
    output_tokens: int = Field(ge=0, default=0)


class AgentTokenLogOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    session_id: int
    model_name: str
    input_tokens: int
    output_tokens: int
    cost: Decimal
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# BillingRate
# ---------------------------------------------------------------------------


class BillingRateCreate(BaseModel):
    user_id: int | None = None
    hourly_rate: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    currency: str = Field(default="USD", max_length=3)
    effective_from: date


class BillingRateUpdate(BaseModel):
    hourly_rate: Decimal | None = Field(None, gt=0, max_digits=10, decimal_places=2)
    currency: str | None = Field(None, max_length=3)
    effective_from: date | None = None


class BillingRateOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    user_id: int | None
    project_id: int | None
    hourly_rate: Decimal
    currency: str
    effective_from: date
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Cost aggregation responses
# ---------------------------------------------------------------------------


class IssueCostOut(BaseModel):
    issue_ref: str
    total_input_tokens: int
    total_output_tokens: int
    total_cost: Decimal


class CostSummaryItem(BaseModel):
    model_name: str
    total_input_tokens: int
    total_output_tokens: int
    total_cost: Decimal


class BillingReportEntry(BaseModel):
    time_entry_id: int
    user_id: int
    hours: Decimal
    rate: Decimal
    amount: Decimal
    spent_on: date


class BillingReportOut(BaseModel):
    total_hours: Decimal
    total_amount: Decimal
    currency: str
    entries: list[BillingReportEntry]
