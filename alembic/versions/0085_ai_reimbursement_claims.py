"""AI reimbursement claims and the usage entries each one froze.

Create Date: 2026-11-23
Revision ID: 0085
Revises: 0081

An employee claims back, once a month, the reimbursable part of the AI usage
they paid for themselves (share x (allocated seat cost + marginal) of
employee-paid entries in company projects).

- ``ai_reimbursement_claims``: ``user_id`` (``ON DELETE RESTRICT``),
  ``month`` (the first day, checked), ``status`` (``draft``, ``submitted``,
  ``approved``, ``paid``, ``rejected``), ``total_amount`` /
  ``total_currency`` (``NUMERIC(20, 8)`` + ``CHAR(3)`` referencing
  ``currencies``), ``entry_count``, ``note``, ``computed_at``,
  ``submitted_at``, ``decided_at`` / ``decided_by_id``, ``decision_note``,
  ``paid_at`` / ``paid_by_id`` (both users ``ON DELETE SET NULL``),
  timestamps. One claim per person and month that is not rejected (a partial
  unique index).
- ``ai_reimbursement_claim_entries``: per claimed entry, ``entry_id``
  (``ON DELETE SET NULL``: a deleted entry stays in the claim, shown as
  changed), ``revision``, ``fingerprint`` (SHA-256 hex of what priced and
  billed the entry), the day, project and billing account (both ``SET
  NULL``), tool, model, payer, cost type, ``share``, and ``marginal_amount``,
  ``seat_amount`` and ``reimbursable_amount`` in ``currency``. Cascades with
  its claim.

Every foreign key has its index. No data to move.
"""

import sqlalchemy as sa

from alembic import op

revision = "0085"
down_revision = "0081"
branch_labels = None
depends_on = None

_STATUSES = "'draft', 'submitted', 'approved', 'paid', 'rejected'"


def _amount(name: str) -> sa.Column:
    return sa.Column(name, sa.Numeric(20, 8), nullable=False)


def _currency(name: str) -> sa.Column:
    return sa.Column(name, sa.CHAR(3), sa.ForeignKey("currencies.code", ondelete="RESTRICT"), nullable=False)


def upgrade() -> None:
    op.create_table(
        "ai_reimbursement_claims",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("month", sa.Date(), nullable=False),
        sa.Column("status", sa.String(10), nullable=False, server_default="draft"),
        _amount("total_amount"),
        _currency("total_currency"),
        sa.Column("entry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_by_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(f"status IN ({_STATUSES})", name="ck_ai_reimbursement_claims_status"),
        sa.CheckConstraint("EXTRACT(DAY FROM month) = 1", name="ck_ai_reimbursement_claims_month_start"),
        sa.CheckConstraint("entry_count >= 0", name="ck_ai_reimbursement_claims_entry_count"),
    )
    op.create_index(
        "uq_ai_reimbursement_claims_user_month",
        "ai_reimbursement_claims",
        ["user_id", "month"],
        unique=True,
        postgresql_where=sa.text("status <> 'rejected'"),
    )
    op.create_index("ix_ai_reimbursement_claims_user_month", "ai_reimbursement_claims", ["user_id", "month"])
    op.create_index("ix_ai_reimbursement_claims_status", "ai_reimbursement_claims", ["status"])
    op.create_index("ix_ai_reimbursement_claims_decided_by_id", "ai_reimbursement_claims", ["decided_by_id"])
    op.create_index("ix_ai_reimbursement_claims_paid_by_id", "ai_reimbursement_claims", ["paid_by_id"])
    op.create_index("ix_ai_reimbursement_claims_total_currency", "ai_reimbursement_claims", ["total_currency"])

    op.create_table(
        "ai_reimbursement_claim_entries",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "claim_id",
            sa.Integer(),
            sa.ForeignKey("ai_reimbursement_claims.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "entry_id", sa.BigInteger(), sa.ForeignKey("ai_usage_entries.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="SET NULL"), nullable=True),
        sa.Column(
            "billing_account_id",
            sa.Integer(),
            sa.ForeignKey("ai_billing_accounts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("tool_slug", sa.String(50), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("payer", sa.String(8), nullable=False),
        sa.Column("cost_type", sa.String(12), nullable=False),
        sa.Column("share", sa.Numeric(5, 4), nullable=False),
        _currency("currency"),
        _amount("marginal_amount"),
        _amount("seat_amount"),
        _amount("reimbursable_amount"),
        sa.CheckConstraint("share >= 0 AND share <= 1", name="ck_ai_reimbursement_claim_entries_share"),
        sa.CheckConstraint("revision >= 1", name="ck_ai_reimbursement_claim_entries_revision"),
    )
    for column in ("claim_id", "entry_id", "project_id", "billing_account_id", "currency"):
        op.create_index(f"ix_ai_reimbursement_claim_entries_{column}", "ai_reimbursement_claim_entries", [column])


def downgrade() -> None:
    op.drop_table("ai_reimbursement_claim_entries")
    op.drop_table("ai_reimbursement_claims")
