"""Schemas of the AI usage routes: the administration range recompute and the ingest contract."""

from __future__ import annotations

from pydantic import AwareDatetime, BaseModel, Field, model_validator


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
