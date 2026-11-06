"""AI model price model: one model's per-1M-token prices during one validity period."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column

from specivo.core.ai_pricing import ModelPrice
from specivo.core.ai_pricing_seed import PRICE_COLUMNS
from specivo.models.base import Base, TimestampMixin
from specivo.models.money import amount_column, currency_column

#: Where a price row came from: the bundled LiteLLM snapshot (and its history
#: file), or an administrator. Admin rows win where both are valid.
AI_MODEL_PRICE_SOURCES: tuple[str, ...] = ("seed", "admin")

#: Columns no UPDATE may change (a trigger enforces it): a price is never
#: edited in place. A change closes the row (``effective_to``) and inserts a
#: new one.
IMMUTABLE_COLUMNS: tuple[str, ...] = (
    "provider",
    "model",
    "currency_code",
    *PRICE_COLUMNS,
    "long_context_threshold",
    "is_local",
    "baseline_model",
    "effective_from",
    "source",
)


class AiModelPrice(Base, TimestampMixin):
    """Prices of one model, per one million tokens, valid during ``[effective_from, effective_to)``.

    The table is a price **history**: each row is one period, ``effective_to``
    ``NULL`` means it is still valid. Periods from one source never overlap
    for a model (an exclusion constraint over the active rows); a ``seed`` and
    an ``admin`` row may, and the admin row wins when prices are resolved.

    Amounts, the currency, the identity and ``effective_from`` cannot be
    updated (trigger ``ai_model_prices_immutable``). What may change in place
    is ``effective_to`` (closing a period), ``is_active``, ``superseded_by_id``,
    ``aliases`` and ``note``. The service refuses to delete a row usage
    entries reference, or to move its end before them.

    ``reasoning`` ``NULL`` bills reasoning tokens at the output price; any
    other ``NULL`` price leaves that token class unpriced. ``long_*`` prices
    apply to requests whose context passed ``long_context_threshold`` tokens.
    A local model (``is_local``) may name a paid ``baseline_model`` that values
    its usage at API prices.
    """

    __tablename__ = "ai_model_prices"

    __table_args__ = (
        CheckConstraint("source IN ('seed', 'admin')", name="ck_ai_model_prices_source"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_ai_model_prices_period",
        ),
        CheckConstraint("model = lower(model) AND model <> ''", name="ck_ai_model_prices_model_normalized"),
        CheckConstraint("provider ~ '^[a-z0-9][a-z0-9_.-]*$'", name="ck_ai_model_prices_provider_format"),
        CheckConstraint(
            "long_context_threshold IS NULL OR long_context_threshold > 0",
            name="ck_ai_model_prices_long_context_threshold",
        ),
        CheckConstraint(
            " AND ".join(f"({c} IS NULL OR {c} >= 0)" for c in PRICE_COLUMNS),
            name="ck_ai_model_prices_prices_non_negative",
        ),
        CheckConstraint(
            "long_context_threshold IS NOT NULL OR ("
            + " AND ".join(f"{c} IS NULL" for c in PRICE_COLUMNS if c.startswith("long_"))
            + ")",
            name="ck_ai_model_prices_long_prices_need_threshold",
        ),
        ExcludeConstraint(
            ("provider", "="),
            ("model", "="),
            ("source", "="),
            (text("tstzrange(effective_from, effective_to, '[)')"), "&&"),
            using="gist",
            where=text("is_active"),
            name="ex_ai_model_prices_no_overlap",
        ),
        Index("ix_ai_model_prices_model_from", "provider", "model", text("effective_from DESC")),
        Index("ix_ai_model_prices_lookup", "model", text("effective_from DESC")),
        Index("ix_ai_model_prices_aliases", "aliases", postgresql_using="gin"),
        Index("ix_ai_model_prices_currency_code", "currency_code"),
        Index("ix_ai_model_prices_created_by_id", "created_by_id"),
        Index("ix_ai_model_prices_superseded_by_id", "superseded_by_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    aliases: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list, server_default=text("'{}'::text[]")
    )
    currency_code: Mapped[str] = currency_column()

    input: Mapped[Decimal] = amount_column("unit_price")
    output: Mapped[Decimal] = amount_column("unit_price")
    cache_write_5m: Mapped[Decimal | None] = amount_column("unit_price", nullable=True)
    cache_write_1h: Mapped[Decimal | None] = amount_column("unit_price", nullable=True)
    cache_read: Mapped[Decimal | None] = amount_column("unit_price", nullable=True)
    reasoning: Mapped[Decimal | None] = amount_column("unit_price", nullable=True)

    long_context_threshold: Mapped[int | None] = mapped_column(Integer, nullable=True)
    long_input: Mapped[Decimal | None] = amount_column("unit_price", nullable=True)
    long_output: Mapped[Decimal | None] = amount_column("unit_price", nullable=True)
    long_cache_write_5m: Mapped[Decimal | None] = amount_column("unit_price", nullable=True)
    long_cache_write_1h: Mapped[Decimal | None] = amount_column("unit_price", nullable=True)
    long_cache_read: Mapped[Decimal | None] = amount_column("unit_price", nullable=True)

    is_local: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    baseline_model: Mapped[str | None] = mapped_column(String(200), nullable=True)

    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source: Mapped[str] = mapped_column(String(10), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    superseded_by_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("ai_model_prices.id", ondelete="SET NULL"), nullable=True
    )
    created_by_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    def to_price(self) -> ModelPrice:
        """The prices as the pure :class:`~specivo.core.ai_pricing.ModelPrice` the cost function takes."""
        return ModelPrice(
            currency=self.currency_code,
            input=self.input,
            output=self.output,
            cache_write_5m=self.cache_write_5m,
            cache_write_1h=self.cache_write_1h,
            cache_read=self.cache_read,
            reasoning=self.reasoning,
            long_context_threshold=self.long_context_threshold,
            long_input=self.long_input,
            long_output=self.long_output,
            long_cache_write_5m=self.long_cache_write_5m,
            long_cache_write_1h=self.long_cache_write_1h,
            long_cache_read=self.long_cache_read,
        )

    def contains(self, at: datetime) -> bool:
        """Whether *at* lies in ``[effective_from, effective_to)``."""
        return self.effective_from <= at and (self.effective_to is None or at < self.effective_to)

    def __repr__(self) -> str:
        return (
            f"<AiModelPrice {self.provider}/{self.model} {self.source} "
            f"[{self.effective_from.isoformat() if self.effective_from else None}, "
            f"{self.effective_to.isoformat() if self.effective_to else None})>"
        )
