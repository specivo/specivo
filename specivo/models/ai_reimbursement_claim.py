"""AI reimbursement claims: what an employee asks to have paid back for one month of AI usage.

A claim is a snapshot. When it is made (and each time its holder refreshes a
draft) it freezes the entries it covers, with their revision, a fingerprint of
what decided their cost and payer, and the amounts in the claim's currency.
Nothing recomputes the amounts afterwards: an entry that is later re-priced,
re-billed, moved, excluded or deleted shows the claim as changed (see
``AiReimbursementClaimService.changes``) rather than altering what was asked
for or approved.

Statuses: ``draft`` -> ``submitted`` -> ``approved`` -> ``paid``; a submitted
claim may be ``rejected`` (final) or withdrawn back to ``draft``. A person has
at most one claim per month that is not rejected.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from specivo.core.money import Money
from specivo.models.base import Base, TimestampMixin
from specivo.models.money import amount_column, currency_column, money_pair_constraints

CLAIM_DRAFT = "draft"
CLAIM_SUBMITTED = "submitted"
CLAIM_APPROVED = "approved"
CLAIM_PAID = "paid"
CLAIM_REJECTED = "rejected"
CLAIM_STATUSES: tuple[str, ...] = (CLAIM_DRAFT, CLAIM_SUBMITTED, CLAIM_APPROVED, CLAIM_PAID, CLAIM_REJECTED)
#: Statuses whose amounts are frozen for good (a draft can still be refreshed).
CLAIM_FROZEN_STATUSES: tuple[str, ...] = (CLAIM_SUBMITTED, CLAIM_APPROVED, CLAIM_PAID)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class AiReimbursementClaim(Base, TimestampMixin):
    """One person's claim for one month (``month`` is the first day of it), in one currency.

    ``total_amount`` is the sum of the entries' reimbursable amounts, stored
    unrounded like every computed amount. ``note`` is the holder's,
    ``decision_note`` the approver's reason for a rejection. Who decided and
    who marked it paid are kept with their times.
    """

    __tablename__ = "ai_reimbursement_claims"
    __table_args__ = (
        CheckConstraint(_in("status", CLAIM_STATUSES), name="ck_ai_reimbursement_claims_status"),
        CheckConstraint("EXTRACT(DAY FROM month) = 1", name="ck_ai_reimbursement_claims_month_start"),
        CheckConstraint("entry_count >= 0", name="ck_ai_reimbursement_claims_entry_count"),
        *money_pair_constraints("ai_reimbursement_claims", "total", nullable=False),
        Index(
            "uq_ai_reimbursement_claims_user_month",
            "user_id",
            "month",
            unique=True,
            postgresql_where=text(f"status <> '{CLAIM_REJECTED}'"),
        ),
        Index("ix_ai_reimbursement_claims_user_month", "user_id", "month"),
        Index("ix_ai_reimbursement_claims_status", "status"),
        Index("ix_ai_reimbursement_claims_decided_by_id", "decided_by_id"),
        Index("ix_ai_reimbursement_claims_paid_by_id", "paid_by_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    month: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default=CLAIM_DRAFT, server_default=CLAIM_DRAFT)
    total_amount: Mapped[Decimal] = amount_column()
    total_currency: Mapped[str] = currency_column()
    entry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_by_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    @property
    def total(self) -> Money:
        return Money(self.total_amount, self.total_currency)

    @property
    def is_frozen(self) -> bool:
        """Whether the amounts can no longer be refreshed (submitted, approved or paid)."""
        return self.status in CLAIM_FROZEN_STATUSES

    def __repr__(self) -> str:
        return f"<AiReimbursementClaim {self.id} user={self.user_id} {self.month:%Y-%m} {self.status}>"


class AiReimbursementClaimEntry(Base):
    """One usage entry as a claim froze it.

    ``entry_id`` is ``NULL`` once the entry was deleted. ``revision`` and
    ``fingerprint`` (a hash of the entry's day, project, billing account,
    payer, cost type, subscription and costs; see
    :func:`specivo.services.ai_reimbursement_claim_service.entry_fingerprint`)
    are what the entry looked like; ``share`` the reimbursed share then.
    ``marginal_amount`` and ``seat_amount`` (the entry's allocated seat cost)
    are converted into the claim's currency (``currency``) on the entry's day;
    ``reimbursable_amount`` = share x (seat + marginal). The descriptive
    columns (day, tool, model, payer, cost type) are kept so the claim still
    reads the same when the entry changes or goes.
    """

    __tablename__ = "ai_reimbursement_claim_entries"
    __table_args__ = (
        CheckConstraint("share >= 0 AND share <= 1", name="ck_ai_reimbursement_claim_entries_share"),
        CheckConstraint("revision >= 1", name="ck_ai_reimbursement_claim_entries_revision"),
        Index("ix_ai_reimbursement_claim_entries_claim_id", "claim_id"),
        Index("ix_ai_reimbursement_claim_entries_entry_id", "entry_id"),
        Index("ix_ai_reimbursement_claim_entries_project_id", "project_id"),
        Index("ix_ai_reimbursement_claim_entries_billing_account_id", "billing_account_id"),
        Index("ix_ai_reimbursement_claim_entries_currency", "currency"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    claim_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("ai_reimbursement_claims.id", ondelete="CASCADE"), nullable=False
    )
    entry_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("ai_usage_entries.id", ondelete="SET NULL"), nullable=True
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    usage_date: Mapped[date] = mapped_column(Date, nullable=False)
    project_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("projects.id", ondelete="SET NULL"), nullable=True
    )
    billing_account_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("ai_billing_accounts.id", ondelete="SET NULL"), nullable=True
    )
    tool_slug: Mapped[str] = mapped_column(String(50), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    payer: Mapped[str] = mapped_column(String(8), nullable=False)
    cost_type: Mapped[str] = mapped_column(String(12), nullable=False)
    share: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    currency: Mapped[str] = currency_column()
    marginal_amount: Mapped[Decimal] = amount_column()
    seat_amount: Mapped[Decimal] = amount_column()
    reimbursable_amount: Mapped[Decimal] = amount_column()

    def __repr__(self) -> str:
        return f"<AiReimbursementClaimEntry claim={self.claim_id} entry={self.entry_id} r{self.revision}>"
