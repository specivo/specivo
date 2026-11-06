"""Pydantic schemas for agent cost tracking endpoints."""

from __future__ import annotations

from datetime import datetime
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
