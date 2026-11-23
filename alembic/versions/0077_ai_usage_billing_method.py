"""How an AI usage entry's billing account was resolved, and the cost type that was asked for.

Create Date: 2026-11-23
Revision ID: 0077
Revises: 0076

Ingest resolves each entry's billing account and payer (the record's
``billing_account`` slug, an instance account's route patterns, the user's own
accounts and subscriptions, then the project's payer default). When an account
changes, the affected entries are resolved again from what was sent, so two
inputs are kept on the entry:

- ``billing_method`` (String(16), NOT NULL, default ``none``, CHECK): the step
  that decided (``manual``, ``record``, ``instance_route``, ``personal_route``,
  ``subscription``, ``project_default``, ``none``).
- ``requested_cost_type`` (String(12), nullable, CHECK): the cost type the
  record or person gave; ``NULL`` when none was given.

Back-fill: an entry charged to a subscription points at that subscription's
billing account, takes its payer and gets ``subscription``, and asked for no
cost type; any other entry keeps no account and ``none``, and asked for its
cost type unless that is ``unknown``.

The downgrade drops both columns and keeps the back-filled accounts and payers.
"""

import sqlalchemy as sa

from alembic import op

revision = "0077"
down_revision = "0076"
branch_labels = None
depends_on = None

_METHODS = ("manual", "record", "instance_route", "personal_route", "subscription", "project_default", "none")
_COST_TYPES = ("subscription", "api", "local", "free", "unknown")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    op.add_column(
        "ai_usage_entries",
        sa.Column("billing_method", sa.String(length=16), nullable=False, server_default="none"),
    )
    op.add_column("ai_usage_entries", sa.Column("requested_cost_type", sa.String(length=12), nullable=True))
    op.create_check_constraint(
        "ck_ai_usage_entries_billing_method", "ai_usage_entries", _in("billing_method", _METHODS)
    )
    op.create_check_constraint(
        "ck_ai_usage_entries_requested_cost_type",
        "ai_usage_entries",
        f"requested_cost_type IS NULL OR {_in('requested_cost_type', _COST_TYPES)}",
    )
    op.execute(
        """
        UPDATE ai_usage_entries
        SET requested_cost_type = cost_type
        WHERE subscription_id IS NULL AND cost_type <> 'unknown'
        """
    )
    op.execute(
        """
        UPDATE ai_usage_entries e
        SET billing_account_id = a.id, payer = a.payer, billing_method = 'subscription'
        FROM ai_subscriptions s
        JOIN ai_billing_accounts a ON a.id = s.billing_account_id
        WHERE e.subscription_id = s.id
        """
    )


def downgrade() -> None:
    op.drop_constraint("ck_ai_usage_entries_requested_cost_type", "ai_usage_entries", type_="check")
    op.drop_constraint("ck_ai_usage_entries_billing_method", "ai_usage_entries", type_="check")
    op.drop_column("ai_usage_entries", "requested_cost_type")
    op.drop_column("ai_usage_entries", "billing_method")
