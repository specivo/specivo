"""AI spend budgets: a monthly budget per billing account or payer, and the alerts sent for it.

Create Date: 2026-11-24
Revision ID: 0088
Revises: 0086

- ``ai_spend_budgets``: a monthly budget on one billing account
  (``billing_account_id``, FK ``ON DELETE CASCADE``) or on everything one payer
  pays (``payer``: ``employer`` or ``employee``); exactly one of the two is set
  (CHECK ``ck_ai_spend_budgets_target``), and each account and each payer has
  at most one budget (two partial unique indexes). ``amount_amount`` /
  ``amount_currency`` (``NUMERIC(20, 8)`` + ``CHAR(3)`` referencing
  ``currencies``, above zero), ``thresholds`` (``integer[]``, the percentages
  that send an alert, 1 to 5 of them, default ``{80,100}``), ``is_active``,
  ``created_by_id`` (FK ``ON DELETE SET NULL``) and timestamps.
- ``ai_spend_budget_alerts``: one row per budget, month (``period_start``, its
  first day, checked) and threshold that was reached, unique on the three, so
  an alert is sent once per threshold and month however often the check runs.
  It keeps the spend it saw (``spend_amount`` / ``spend_currency``), the budget
  amount at that moment, how many people were mailed and when. Cascades with
  its budget.

Every foreign key has its index. No data to move.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0088"
down_revision = "0086"
branch_labels = None
depends_on = None


def _amount(name: str) -> sa.Column:
    return sa.Column(name, sa.Numeric(20, 8), nullable=False)


def _currency(name: str) -> sa.Column:
    return sa.Column(name, sa.CHAR(3), sa.ForeignKey("currencies.code", ondelete="RESTRICT"), nullable=False)


def upgrade() -> None:
    op.create_table(
        "ai_spend_budgets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "billing_account_id",
            sa.Integer(),
            sa.ForeignKey("ai_billing_accounts.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("payer", sa.String(8), nullable=True),
        _amount("amount_amount"),
        _currency("amount_currency"),
        sa.Column(
            "thresholds",
            postgresql.ARRAY(sa.Integer()),
            nullable=False,
            server_default=sa.text("'{80,100}'::integer[]"),
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("(billing_account_id IS NULL) <> (payer IS NULL)", name="ck_ai_spend_budgets_target"),
        sa.CheckConstraint("payer IS NULL OR payer IN ('employer', 'employee')", name="ck_ai_spend_budgets_payer"),
        sa.CheckConstraint("amount_amount > 0", name="ck_ai_spend_budgets_amount_positive"),
        sa.CheckConstraint(
            "cardinality(thresholds) BETWEEN 1 AND 5 AND 0 < ALL(thresholds) AND 1000 >= ALL(thresholds)",
            name="ck_ai_spend_budgets_thresholds",
        ),
    )
    op.create_index(
        "uq_ai_spend_budgets_billing_account",
        "ai_spend_budgets",
        ["billing_account_id"],
        unique=True,
        postgresql_where=sa.text("billing_account_id IS NOT NULL"),
    )
    op.create_index(
        "uq_ai_spend_budgets_payer",
        "ai_spend_budgets",
        ["payer"],
        unique=True,
        postgresql_where=sa.text("payer IS NOT NULL"),
    )
    op.create_index("ix_ai_spend_budgets_amount_currency", "ai_spend_budgets", ["amount_currency"])
    op.create_index("ix_ai_spend_budgets_created_by_id", "ai_spend_budgets", ["created_by_id"])

    op.create_table(
        "ai_spend_budget_alerts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("budget_id", sa.Integer(), sa.ForeignKey("ai_spend_budgets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("threshold", sa.Integer(), nullable=False),
        _amount("spend_amount"),
        _currency("spend_currency"),
        _amount("budget_amount"),
        sa.Column("recipients", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("EXTRACT(DAY FROM period_start) = 1", name="ck_ai_spend_budget_alerts_period_start"),
        sa.CheckConstraint("threshold > 0", name="ck_ai_spend_budget_alerts_threshold"),
        sa.UniqueConstraint("budget_id", "period_start", "threshold", name="uq_ai_spend_budget_alerts_once"),
    )
    op.create_index("ix_ai_spend_budget_alerts_spend_currency", "ai_spend_budget_alerts", ["spend_currency"])


def downgrade() -> None:
    op.drop_table("ai_spend_budget_alerts")
    op.drop_table("ai_spend_budgets")
