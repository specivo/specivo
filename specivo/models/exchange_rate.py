"""Exchange rate model: one daily rate between two currencies from one source."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CHAR,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from specivo.models.base import Base
from specivo.models.money import money_numeric

#: Where a rate came from, in the order a rate of the same day wins:
#: typed by an administrator, imported from a CSV file, fetched from the ECB
#: (or a compatible feed such as a self-hosted Frankfurter).
EXCHANGE_RATE_SOURCES: tuple[str, ...] = ("manual", "csv", "ecb")


class ExchangeRate(Base):
    """``1 base_code = rate quote_code`` on ``rate_date``, as published by ``source``.

    Rates are daily: one row per day, pair and source. Several sources may hold
    a rate for the same day and pair; the conversion service prefers
    ``manual``, then ``csv``, then ``ecb``. The inverse pair is not stored: the
    service derives it (``1 / rate``).
    """

    __tablename__ = "exchange_rates"

    __table_args__ = (
        UniqueConstraint("rate_date", "base_code", "quote_code", "source", name="uq_exchange_rates_date_pair_source"),
        CheckConstraint("base_code <> quote_code", name="ck_exchange_rates_distinct_pair"),
        CheckConstraint("rate > 0", name="ck_exchange_rates_rate_positive"),
        CheckConstraint("source IN ('manual', 'csv', 'ecb')", name="ck_exchange_rates_source"),
        # Lookups read the newest rate of a pair on or before a day.
        Index("ix_exchange_rates_pair_date", "base_code", "quote_code", text("rate_date DESC")),
        Index("ix_exchange_rates_quote_code", "quote_code"),
        Index("ix_exchange_rates_created_by_id", "created_by_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rate_date: Mapped[date] = mapped_column(Date, nullable=False)
    base_code: Mapped[str] = mapped_column(CHAR(3), ForeignKey("currencies.code", ondelete="RESTRICT"), nullable=False)
    quote_code: Mapped[str] = mapped_column(CHAR(3), ForeignKey("currencies.code", ondelete="RESTRICT"), nullable=False)
    rate: Mapped[Decimal] = mapped_column(money_numeric("rate"), nullable=False)
    source: Mapped[str] = mapped_column(String(10), nullable=False)
    created_by_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    def __repr__(self) -> str:
        return f"<ExchangeRate {self.rate_date} 1 {self.base_code} = {self.rate} {self.quote_code} ({self.source})>"
