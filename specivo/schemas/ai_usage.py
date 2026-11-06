"""Schemas of the AI usage administration routes (the range recompute)."""

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
