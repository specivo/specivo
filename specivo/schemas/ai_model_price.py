"""AI model price schemas.

Prices are per one million tokens and cross the API as **strings** (``"3.75"``),
never JSON numbers, like every amount (see :mod:`specivo.schemas.money`).
Instants are ISO 8601 with an offset; responses give them in UTC.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictStr

from specivo.core.ai_pricing_seed import PRICE_COLUMNS, format_price
from specivo.models.ai_model_price import AiModelPrice
from specivo.schemas.money import CurrencyCode
from specivo.services.ai_model_price_seed import SeedOutcome
from specivo.services.ai_model_price_service import ResolvedPrice

PriceText = StrictStr


class AiModelPriceValues(BaseModel):
    """Per-1M-token prices; ``input`` and ``output`` are required, the others optional.

    ``reasoning`` empty bills reasoning tokens at the output price; any other
    empty price leaves its token class unpriced. ``long_*`` prices need
    ``long_context_threshold``.
    """

    model_config = ConfigDict(extra="forbid")

    input: PriceText = Field(examples=["3"])
    output: PriceText = Field(examples=["15"])
    cache_write_5m: PriceText | None = Field(None, examples=["3.75"])
    cache_write_1h: PriceText | None = Field(None, examples=["6"])
    cache_read: PriceText | None = Field(None, examples=["0.3"])
    reasoning: PriceText | None = None
    long_input: PriceText | None = None
    long_output: PriceText | None = None
    long_cache_write_5m: PriceText | None = None
    long_cache_write_1h: PriceText | None = None
    long_cache_read: PriceText | None = None


class AiModelPriceCreate(BaseModel):
    """A new administrator price period: "new price from ``effective_from``".

    Without ``effective_to`` the period is open, and it closes the open admin
    period of the same model at ``effective_from``. With ``effective_to`` it is
    a past (or bounded) period, which must not overlap another admin period.
    """

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(max_length=50, examples=["anthropic"])
    model: str = Field(max_length=250, examples=["claude-sonnet-4-5"])
    currency: CurrencyCode
    prices: AiModelPriceValues
    effective_from: AwareDatetime
    effective_to: AwareDatetime | None = None
    long_context_threshold: int | None = Field(None, gt=0, examples=[200000])
    aliases: list[str] = Field(default_factory=list, max_length=20)
    is_local: bool = False
    baseline_model: str | None = Field(None, max_length=250)
    note: str | None = Field(None, max_length=1000)


class AiModelPriceUpdate(BaseModel):
    """Fields that can change in place. Prices, currency and start never do.

    Send ``effective_to: null`` to reopen a period; leave a field out to keep it.
    """

    model_config = ConfigDict(extra="forbid")

    effective_to: AwareDatetime | None = None
    is_active: bool | None = None
    aliases: list[str] | None = Field(None, max_length=20)
    note: str | None = Field(None, max_length=1000)


def _iso(value: datetime | None) -> datetime | None:
    return value.astimezone(UTC) if value is not None else None


class AiModelPriceOut(BaseModel):
    id: int
    provider: str
    model: str
    aliases: list[str]
    currency: str
    prices: dict[str, str | None] = Field(description="Per-1M-token prices as decimal strings; null = not priced")
    long_context_threshold: int | None
    is_local: bool
    baseline_model: str | None
    effective_from: datetime
    effective_to: datetime | None
    source: str
    is_active: bool
    superseded_by_id: int | None
    created_by_id: int | None
    note: str | None
    created_at: datetime | None
    updated_at: datetime | None

    @classmethod
    def from_row(cls, row: AiModelPrice) -> AiModelPriceOut:
        return cls(
            id=row.id,
            provider=row.provider,
            model=row.model,
            aliases=list(row.aliases or []),
            currency=row.currency_code,
            prices={column: format_price(getattr(row, column)) for column in PRICE_COLUMNS},
            long_context_threshold=row.long_context_threshold,
            is_local=row.is_local,
            baseline_model=row.baseline_model,
            effective_from=_iso(row.effective_from),  # type: ignore[arg-type]
            effective_to=_iso(row.effective_to),
            source=row.source,
            is_active=row.is_active,
            superseded_by_id=row.superseded_by_id,
            created_by_id=row.created_by_id,
            note=row.note,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class AiModelPriceCreatedOut(AiModelPriceOut):
    closed_price_id: int | None = Field(None, description="The open period this price closed, if any")


class AiModelPriceResolveOut(BaseModel):
    """``priced`` with the price valid at ``at``, or ``unpriced`` (never a fallback to the latest price)."""

    status: str = Field(examples=["priced", "unpriced"])
    model: str = Field(description="The normalised name that was looked up")
    at: datetime
    matched_by: str | None = Field(None, examples=["model", "alias"])
    price: AiModelPriceOut | None = None

    @classmethod
    def build(cls, model: str, at: datetime, resolved: ResolvedPrice | None) -> AiModelPriceResolveOut:
        if resolved is None:
            return cls(status="unpriced", model=model, at=at.astimezone(UTC), matched_by=None, price=None)
        return cls(
            status="priced",
            model=resolved.model,
            at=resolved.at,
            matched_by=resolved.matched_by,
            price=AiModelPriceOut.from_row(resolved.price),
        )


class AiModelPriceSeedOut(BaseModel):
    as_of: datetime
    inserted: int
    closed: int
    history_inserted: int
    aliases_updated: int
    unchanged: int
    skipped: list[str]

    @classmethod
    def from_outcome(cls, outcome: SeedOutcome) -> AiModelPriceSeedOut:
        return cls(
            as_of=outcome.as_of,
            inserted=outcome.inserted,
            closed=outcome.closed,
            history_inserted=outcome.history_inserted,
            aliases_updated=outcome.aliases_updated,
            unchanged=outcome.unchanged,
            skipped=list(outcome.skipped),
        )


def prices_dict(values: AiModelPriceValues) -> dict[str, Any]:
    return values.model_dump()
