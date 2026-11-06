"""Billing rate model: an hourly rate in one currency, per project or per user in a project."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import Date, ForeignKey, Index, Integer
from sqlalchemy.orm import Mapped, mapped_column

from specivo.core.money import Money
from specivo.models.base import Base, TimestampMixin
from specivo.models.money import amount_column, currency_column


class BillingRate(Base, TimestampMixin):
    """An hourly billing rate from ``effective_from`` on.

    ``user_id`` NULL is the project's rate; a row with a user overrides it for
    that user. ``hourly_rate`` follows the money convention (``NUMERIC(20, 8)``
    plus a ``CHAR(3)`` currency referencing ``currencies.code``). The amount is
    validated to the currency's ISO minor units when someone enters it, never
    rounded in storage.
    """

    __tablename__ = "billing_rates"

    __table_args__ = (Index("ix_billing_rates_currency_code", "currency_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True
    )
    hourly_rate: Mapped[Decimal] = amount_column("amount")
    currency_code: Mapped[str] = currency_column()
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)

    @property
    def rate(self) -> Money:
        """The hourly rate as :class:`Money`."""
        return Money(self.hourly_rate, self.currency_code)

    def __repr__(self) -> str:
        return f"<BillingRate id={self.id} hourly_rate={self.hourly_rate} currency_code={self.currency_code!r}>"
