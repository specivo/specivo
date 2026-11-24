"""AI spend budgets: a monthly budget on a billing account or on everything one payer pays, and its alerts.

See :mod:`specivo.services.ai_spend_budget_service` for how spend is measured
and when an alert is sent.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
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
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from specivo.core.ai_billing import PAYER_EMPLOYEE, PAYER_EMPLOYER
from specivo.core.money import Money
from specivo.models.base import Base, TimestampMixin
from specivo.models.money import amount_column, currency_column, money_pair_constraints

#: Payers a budget can cover as a whole (``unknown`` is nobody's to budget).
BUDGET_PAYERS: tuple[str, ...] = (PAYER_EMPLOYER, PAYER_EMPLOYEE)
#: The thresholds (percent of the budget) a new budget alerts at.
DEFAULT_THRESHOLDS: tuple[int, ...] = (80, 100)
#: Most thresholds one budget has, and the highest one (ten times the budget).
MAX_THRESHOLDS = 5
MAX_THRESHOLD = 1000


class AiSpendBudget(Base, TimestampMixin):
    """A monthly budget on one billing account (``billing_account_id``) or one payer's whole spend (``payer``).

    Exactly one of the two is set. ``amount`` is the monthly budget in its
    currency; ``thresholds`` the percentages of it that send an alert, once
    each per calendar month (UTC). A budget on a personal account belongs to
    that account's owner; budgets on instance accounts and on a payer are the
    administrators'.
    """

    __tablename__ = "ai_spend_budgets"
    __table_args__ = (
        CheckConstraint("(billing_account_id IS NULL) <> (payer IS NULL)", name="ck_ai_spend_budgets_target"),
        CheckConstraint(
            f"payer IS NULL OR payer IN ({', '.join(repr(p) for p in BUDGET_PAYERS)})",
            name="ck_ai_spend_budgets_payer",
        ),
        CheckConstraint("amount_amount > 0", name="ck_ai_spend_budgets_amount_positive"),
        CheckConstraint(
            f"cardinality(thresholds) BETWEEN 1 AND {MAX_THRESHOLDS} AND 0 < ALL(thresholds)"
            f" AND {MAX_THRESHOLD} >= ALL(thresholds)",
            name="ck_ai_spend_budgets_thresholds",
        ),
        Index(
            "uq_ai_spend_budgets_billing_account",
            "billing_account_id",
            unique=True,
            postgresql_where=text("billing_account_id IS NOT NULL"),
        ),
        Index("uq_ai_spend_budgets_payer", "payer", unique=True, postgresql_where=text("payer IS NOT NULL")),
        *money_pair_constraints("ai_spend_budgets", "amount", nullable=False),
        Index("ix_ai_spend_budgets_created_by_id", "created_by_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    billing_account_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("ai_billing_accounts.id", ondelete="CASCADE"), nullable=True
    )
    payer: Mapped[str | None] = mapped_column(String(8), nullable=True)
    amount_amount: Mapped[Decimal] = amount_column()
    amount_currency: Mapped[str] = currency_column()
    thresholds: Mapped[list[int]] = mapped_column(
        ARRAY(Integer),
        nullable=False,
        default=lambda: list(DEFAULT_THRESHOLDS),
        server_default=text("'{80,100}'::integer[]"),
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    created_by_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    @property
    def amount(self) -> Money:
        return Money(self.amount_amount, self.amount_currency)

    def __repr__(self) -> str:
        target = f"account={self.billing_account_id}" if self.billing_account_id is not None else f"payer={self.payer}"
        return f"<AiSpendBudget {self.id} {target} {self.amount_amount} {self.amount_currency}>"


class AiSpendBudgetAlert(Base):
    """An alert sent for a budget: the month (``period_start``, its first day) and the threshold reached.

    Unique per budget, month and threshold: the check inserts the row before
    it mails, and mails only when the insert went through.
    """

    __tablename__ = "ai_spend_budget_alerts"
    __table_args__ = (
        CheckConstraint("EXTRACT(DAY FROM period_start) = 1", name="ck_ai_spend_budget_alerts_period_start"),
        CheckConstraint("threshold > 0", name="ck_ai_spend_budget_alerts_threshold"),
        UniqueConstraint("budget_id", "period_start", "threshold", name="uq_ai_spend_budget_alerts_once"),
        Index("ix_ai_spend_budget_alerts_spend_currency", "spend_currency"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    budget_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("ai_spend_budgets.id", ondelete="CASCADE"), nullable=False
    )
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    threshold: Mapped[int] = mapped_column(Integer, nullable=False)
    spend_amount: Mapped[Decimal] = amount_column()
    spend_currency: Mapped[str] = currency_column()
    budget_amount: Mapped[Decimal] = amount_column()
    recipients: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    def __repr__(self) -> str:
        return f"<AiSpendBudgetAlert budget={self.budget_id} {self.period_start} {self.threshold}%>"
