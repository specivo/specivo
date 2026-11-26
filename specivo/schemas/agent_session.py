"""Pydantic schemas for AgentSession endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from specivo.schemas.money import MoneyOut


class AgentSessionOut(BaseModel):
    """One agent session: an activity window of an MCP connection, or of a key's plain REST calls."""

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
    tool_call_count: int = 0
    created_at: datetime
    updated_at: datetime


class AgentSessionDetailOut(AgentSessionOut):
    """An agent session with the number of AI usage entries the caller may read for it."""

    usage_entry_count: int = 0


class AgentSessionCostOut(BaseModel):
    """The AI usage of an issue's agent session block that the caller may read, in the reporting currency.

    ``spend`` is the marginal spend and ``api_equivalent`` the API-equivalent
    value of the entries, without seat cost; amounts without an exchange rate
    are left out of both.
    """

    entries: int
    tokens: int
    currency: str
    spend: MoneyOut | None = None
    api_equivalent: MoneyOut | None = None
    profiles: list[str] = Field(default_factory=list)
    unattended: bool = False


class IssueAgentSessionOut(BaseModel):
    """One agent session that worked on an issue: a lineage root with its subagents, counters and cost."""

    id: int = Field(description="The root session's id (its first row when several share a client session id)")
    session_ids: list[int] = Field(description="Every session row of the root (MCP and webhook rows of one run)")
    user_id: int
    user_login: str
    service_account: bool
    client_name: str | None
    client_version: str | None
    client_types: list[str]
    external_session_id: str | None
    started_at: datetime
    last_activity_at: datetime
    ended_at: datetime | None
    state: Literal["running", "completed", "interrupted", "idle"]
    tool_calls: int = Field(description="Tool calls in the block's audit rows of this project, subagents included")
    approvals_requested: int
    approvals_resolved: dict[str, int] = Field(description="Resolved approvals per choice")
    children: list[int] = Field(description="The subagent sessions' ids, flattened to one level")
    cost: AgentSessionCostOut | None = Field(
        None, description="Absent without view_ai_usage in the project, or when no usage was reported yet"
    )
