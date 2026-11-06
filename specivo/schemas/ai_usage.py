"""Schemas of the AI usage routes: entries, the administration range recompute and the ingest contract."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from specivo.core.ai_usage import COMMENTS_MAX_LENGTH, MAX_REQUEST_COUNT, MAX_TOKENS_PER_CLASS
from specivo.schemas.common import IdName, PaginatedResponse
from specivo.schemas.money import MoneyIn, MoneyOut

AiUsageCostType = Literal["subscription", "api", "local", "free", "unknown"]

_COUNT = Field(0, ge=0, le=MAX_TOKENS_PER_CLASS)


# ---------------------------------------------------------------------------
# Entries
# ---------------------------------------------------------------------------


class AiUsageTokens(BaseModel):
    """Token counts per class. Classes left out are 0 (on PATCH: unchanged)."""

    model_config = ConfigDict(extra="forbid")

    input: int = _COUNT
    output: int = _COUNT
    cache_write_5m: int = Field(default=0, ge=0, le=MAX_TOKENS_PER_CLASS, description="Cache writes kept 5 minutes")
    cache_write_1h: int = Field(default=0, ge=0, le=MAX_TOKENS_PER_CLASS, description="Cache writes kept 1 hour")
    cache_read: int = _COUNT
    reasoning: int = _COUNT


class AiUsageCreate(BaseModel):
    """One entry of AI usage, logged by hand or by a client.

    The server prices it from the tokens and its price table. ``cost`` is the
    amount actually paid, if known; it is the spend only for ``cost_type``
    ``api``. Leave ``cost_type`` out to have it derived from the user's AI
    subscriptions (``subscription`` when one covers the tool, else ``unknown``).
    """

    model_config = ConfigDict(extra="forbid")

    tool: str = Field(min_length=1, max_length=50, examples=["claude-code"], description="AI tool slug")
    model: str = Field(min_length=1, max_length=200, examples=["claude-sonnet-4-5"])
    provider: str | None = Field(None, max_length=50, examples=["anthropic"])
    usage_date: date = Field(description="The UTC day of the usage")
    started_at: AwareDatetime | None = None
    ended_at: AwareDatetime | None = None
    request_count: int = Field(0, ge=0, le=MAX_REQUEST_COUNT)
    tokens: AiUsageTokens = Field(default_factory=lambda: AiUsageTokens())
    long_context_tokens: AiUsageTokens | None = Field(
        None, description="The part of the tokens billed at the long-context tier"
    )
    cost_type: AiUsageCostType | None = None
    cost: MoneyIn | None = Field(None, description="What the usage cost, if known (stored as the reported cost)")
    issue_id: int | None = Field(None, description="An issue of the entry's project")
    on_behalf_of: str | None = Field(
        None,
        max_length=255,
        description=(
            "Login of the user in whose name the usage is logged; needs log_ai_usage_for_others in the project "
            "(administrators only for unassigned usage)"
        ),
    )
    comments: str | None = Field(None, max_length=COMMENTS_MAX_LENGTH)
    external_session_id: str | None = Field(None, max_length=200)
    git_branch: str | None = Field(None, max_length=255)
    repo_hint: str | None = Field(None, max_length=255)


class AiUsageUpdate(BaseModel):
    """Partial update of an entry. Only fields sent change; ``null`` clears an optional one.

    ``project_key`` moves the entry (``null``: unassigned; its issue is
    dropped unless ``issue_id`` is sent too). Reporter and import entries only
    take ``project_key``, ``issue_id`` and ``comments`` (422
    ``ai_usage_field_locked`` otherwise). ``tokens`` changes only the classes
    sent.
    """

    model_config = ConfigDict(extra="forbid")

    project_key: str | None = Field(None, max_length=50)
    issue_id: int | None = None
    comments: str | None = Field(None, max_length=COMMENTS_MAX_LENGTH)
    tool: str | None = Field(None, min_length=1, max_length=50)
    model: str | None = Field(None, min_length=1, max_length=200)
    provider: str | None = Field(None, max_length=50)
    usage_date: date | None = None
    started_at: AwareDatetime | None = None
    ended_at: AwareDatetime | None = None
    request_count: int | None = Field(None, ge=0, le=MAX_REQUEST_COUNT)
    tokens: AiUsageTokens | None = None
    long_context_tokens: AiUsageTokens | None = None
    cost_type: AiUsageCostType | None = None
    cost: MoneyIn | None = None
    external_session_id: str | None = Field(None, max_length=200)
    git_branch: str | None = Field(None, max_length=255)
    repo_hint: str | None = Field(None, max_length=255)


class AiUsageUserOut(IdName):
    login: str


class AiUsageEntryOut(BaseModel):
    """An AI usage entry.

    ``user`` is in whose name it is logged, ``reporter`` (and
    ``reporter_api_key_id``) the account and key that wrote it. Costs are the
    snapshot taken when the entry was written or last recomputed: ``marginal_cost``
    is what the usage cost on top of what is paid anyway (null when unknown),
    ``api_equivalent_cost`` what it would have cost at API prices. ``issue_id``
    and ``issue_key`` are null when the reader cannot see the issue.
    """

    id: int
    project_id: int | None
    project_key: str | None
    issue_id: int | None
    issue_key: str | None
    user: AiUsageUserOut
    reporter: AiUsageUserOut
    reporter_api_key_id: int | None
    source: str = Field(description="manual, api, mcp, reporter or import")
    locked: bool = Field(description="Reporter and import entries: only the project, issue and comments can change")
    attribution_method: str
    tool: str
    provider: str | None
    model: str
    usage_date: date
    started_at: datetime | None
    ended_at: datetime | None
    request_count: int
    tokens: AiUsageTokens
    long_context_tokens: AiUsageTokens | None
    cost_type: str
    subscription_id: int | None
    cost_basis: str = Field(description="calculated, reported, manual, unpriced or partial")
    marginal_cost: MoneyOut | None
    api_equivalent_cost: MoneyOut | None
    reported_cost: MoneyOut | None
    price_id: int | None
    priced_at: datetime
    external_session_id: str | None
    git_branch: str | None
    repo_hint: str | None
    client_name: str | None
    client_version: str | None
    comments: str | None
    idempotency_key: str | None
    revision: int
    excluded: bool
    excluded_at: datetime | None
    excluded_by_id: int | None
    created_at: datetime
    updated_at: datetime


class AiUsageListResponse(PaginatedResponse[AiUsageEntryOut]):
    """A page of AI usage entries, newest day first."""


class AiUsageRecomputeIn(BaseModel):
    """Recompute the costs of the entries priced in ``[from, to)``, optionally for one provider or model."""

    model_config = {"populate_by_name": True}

    start: AwareDatetime = Field(alias="from", description="First instant of the range (inclusive)")
    end: AwareDatetime = Field(alias="to", description="End of the range (exclusive)")
    provider: str | None = Field(None, max_length=50)
    model: str | None = Field(None, max_length=250, description="Also matches the aliases of its prices")

    @model_validator(mode="after")
    def _ordered(self) -> AiUsageRecomputeIn:
        if self.end <= self.start:
            raise ValueError("'to' must be after 'from'")
        return self


class AiUsageRecomputeOut(BaseModel):
    """The queued recompute: its task id and how many entries the range holds now."""

    task_id: str
    entries: int


# ---------------------------------------------------------------------------
# Ingest contract v1 (docs/contracts/ai-usage/v1/)
# ---------------------------------------------------------------------------


class AiUsageIngestRateLimitOut(BaseModel):
    requests: int
    window_seconds: int


class AiUsageIngestInfoOut(BaseModel):
    """What the ingest endpoint accepts (``info.schema.json``)."""

    versions: list[str] = Field(description="Contract versions accepted in the batch's 'schema', newest first")
    max_records: int
    max_bytes: int
    max_age_days: int
    rate_limit: AiUsageIngestRateLimitOut | None
    server_time: AwareDatetime
    tools: list[str] = Field(description="Slugs of the active AI tools; other valid slugs are accepted too")
    currencies: list[str] = Field(description="Currency codes accepted in reported_cost")


class AiUsageIngestProblemOut(BaseModel):
    code: str
    message: str
    field: str | None = None


class AiUsageIngestAttributionOut(BaseModel):
    method: str
    project_key: str | None = None
    issue_ref: str | None = None


class AiUsageIngestRecordOut(BaseModel):
    index: int
    idempotency_key: str | None
    status: str = Field(description="created, updated, unchanged or rejected")
    entry_id: int | None
    revision: int | None
    attribution: AiUsageIngestAttributionOut | None
    warnings: list[AiUsageIngestProblemOut]
    error: AiUsageIngestProblemOut | None


class AiUsageIngestSummaryOut(BaseModel):
    received: int
    created: int
    updated: int
    unchanged: int
    rejected: int


class AiUsageIngestOut(BaseModel):
    """The response to a batch (``result.schema.json``): one result per record, in order."""

    schema_: str = Field(alias="schema", serialization_alias="schema")
    server_time: AwareDatetime
    summary: AiUsageIngestSummaryOut
    results: list[AiUsageIngestRecordOut]
    warnings: list[AiUsageIngestProblemOut]

    model_config = {"populate_by_name": True}
