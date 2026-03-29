"""Pydantic schemas for agent groups and group policies."""

from datetime import datetime

from pydantic import BaseModel


class AgentGroupCreate(BaseModel):
    name: str
    description: str | None = None


class AgentGroupOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    name: str
    description: str | None
    created_at: datetime
    updated_at: datetime


class MemberAdd(BaseModel):
    user_id: int


class MembershipOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    group_id: int
    user_id: int
    created_at: datetime


class PolicyCreate(BaseModel):
    project_id: int | None = None
    scopes: list[str]
    ip_allowlist: list[str] | None = None


class PolicyOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    group_id: int
    project_id: int | None
    scopes: list[str]
    ip_allowlist: list[str] | None
    created_at: datetime
    updated_at: datetime
