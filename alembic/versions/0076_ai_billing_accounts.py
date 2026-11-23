"""AI billing accounts; payer, billing account and route on AI usage entries; subscriptions link to an account.

Create Date: 2026-11-23
Revision ID: 0076
Revises: 0075

Who pays for AI usage (employer or employee) and what it was paid through
(a corporate gateway, a company plan, a personal subscription or key, a local
model) sit beside ``cost_type``, which stays the pricing mechanism.

- New table ``ai_billing_accounts``: ``slug`` (unique per owner, and among
  instance accounts), ``name``, ``kind`` (``gateway`` / ``subscription`` /
  ``api_key`` / ``local``), ``owner_user_id`` (FK ``users`` ON DELETE CASCADE;
  ``NULL`` = instance account), ``payer`` (``employer`` / ``employee``),
  ``reimburse_share`` (``NUMERIC(5, 4)``, 0 to 1, employee accounts only),
  ``route_patterns`` and ``tool_slugs`` (``text[]``), ``default_cost_type``
  (nullable), ``is_active``, ``created_by_id`` and timestamps.
- ``ai_subscriptions.billing_account_id`` (FK ON DELETE SET NULL). Back-fill:
  one personal account per existing subscription, of kind ``subscription``,
  paid by the employee, slug ``<plan_key>-<id>``, named after the plan, with
  ``default_cost_type`` ``subscription`` and no tools (the subscription keeps
  its own coverage).
- ``ai_usage_entries``: ``billing_account_id`` (FK ON DELETE SET NULL),
  ``payer`` (``employer`` / ``employee`` / ``unknown``, default ``unknown``,
  CHECK) and ``route`` (String(100)). Existing entries get ``unknown`` and no
  account.

The project setting ``ai_usage_payer_default`` lives in the ``projects.settings``
bag and needs no schema change.

The downgrade drops the new columns and the table; the accounts are lost.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0076"
down_revision = "0075"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_billing_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("kind", sa.String(length=12), nullable=False),
        sa.Column(
            "owner_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE", name="fk_ai_billing_accounts_owner_user_id"),
            nullable=True,
        ),
        sa.Column("payer", sa.String(length=8), nullable=False),
        sa.Column("reimburse_share", sa.Numeric(5, 4), nullable=True),
        sa.Column(
            "route_patterns",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column(
            "tool_slugs",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column("default_cost_type", sa.String(length=12), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column(
            "created_by_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL", name="fk_ai_billing_accounts_created_by_id"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("slug ~ '^[a-z0-9][a-z0-9_.-]{0,63}$'", name="ck_ai_billing_accounts_slug_format"),
        sa.CheckConstraint("btrim(name) <> ''", name="ck_ai_billing_accounts_name_not_blank"),
        sa.CheckConstraint(
            "kind IN ('gateway', 'subscription', 'api_key', 'local')", name="ck_ai_billing_accounts_kind"
        ),
        sa.CheckConstraint("payer IN ('employer', 'employee')", name="ck_ai_billing_accounts_payer"),
        sa.CheckConstraint(
            "reimburse_share IS NULL OR (payer = 'employee' AND reimburse_share >= 0 AND reimburse_share <= 1)",
            name="ck_ai_billing_accounts_reimburse_share",
        ),
        sa.CheckConstraint(
            "default_cost_type IS NULL OR default_cost_type IN ('subscription', 'api', 'local', 'free')",
            name="ck_ai_billing_accounts_default_cost_type",
        ),
    )
    op.create_index(
        "uq_ai_billing_accounts_owner_slug",
        "ai_billing_accounts",
        ["owner_user_id", "slug"],
        unique=True,
        postgresql_where=sa.text("owner_user_id IS NOT NULL"),
    )
    op.create_index(
        "uq_ai_billing_accounts_instance_slug",
        "ai_billing_accounts",
        ["slug"],
        unique=True,
        postgresql_where=sa.text("owner_user_id IS NULL"),
    )
    op.create_index("ix_ai_billing_accounts_created_by_id", "ai_billing_accounts", ["created_by_id"], unique=False)

    # -- subscriptions: one personal account each ------------------------------
    op.add_column(
        "ai_subscriptions",
        sa.Column(
            "billing_account_id",
            sa.Integer(),
            sa.ForeignKey("ai_billing_accounts.id", ondelete="SET NULL", name="fk_ai_subscriptions_billing_account_id"),
            nullable=True,
        ),
    )
    op.create_index("ix_ai_subscriptions_billing_account_id", "ai_subscriptions", ["billing_account_id"], unique=False)
    op.execute(
        """
        WITH created AS (
            INSERT INTO ai_billing_accounts
                (slug, name, kind, owner_user_id, payer, default_cost_type, is_active, created_by_id)
            SELECT s.plan_key || '-' || s.id, s.plan_name, 'subscription', s.user_id, 'employee',
                   'subscription', true, s.created_by_id
            FROM ai_subscriptions s
            ORDER BY s.id
            RETURNING id, owner_user_id, slug
        )
        UPDATE ai_subscriptions s
        SET billing_account_id = created.id
        FROM created
        WHERE created.owner_user_id = s.user_id AND created.slug = s.plan_key || '-' || s.id
        """
    )

    # -- usage entries ---------------------------------------------------------
    op.add_column(
        "ai_usage_entries",
        sa.Column(
            "billing_account_id",
            sa.Integer(),
            sa.ForeignKey("ai_billing_accounts.id", ondelete="SET NULL", name="fk_ai_usage_entries_billing_account_id"),
            nullable=True,
        ),
    )
    op.add_column(
        "ai_usage_entries",
        sa.Column("payer", sa.String(length=8), nullable=False, server_default="unknown"),
    )
    op.add_column("ai_usage_entries", sa.Column("route", sa.String(length=100), nullable=True))
    op.create_check_constraint(
        "ck_ai_usage_entries_payer",
        "ai_usage_entries",
        "payer IN ('employer', 'employee', 'unknown')",
    )
    op.create_index("ix_ai_usage_entries_billing_account_id", "ai_usage_entries", ["billing_account_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_ai_usage_entries_billing_account_id", table_name="ai_usage_entries")
    op.drop_constraint("ck_ai_usage_entries_payer", "ai_usage_entries", type_="check")
    op.drop_column("ai_usage_entries", "route")
    op.drop_column("ai_usage_entries", "payer")
    op.drop_column("ai_usage_entries", "billing_account_id")

    op.drop_index("ix_ai_subscriptions_billing_account_id", table_name="ai_subscriptions")
    op.drop_column("ai_subscriptions", "billing_account_id")

    op.drop_index("ix_ai_billing_accounts_created_by_id", table_name="ai_billing_accounts")
    op.drop_index("uq_ai_billing_accounts_instance_slug", table_name="ai_billing_accounts")
    op.drop_index("uq_ai_billing_accounts_owner_slug", table_name="ai_billing_accounts")
    op.drop_table("ai_billing_accounts")
