"""AI tools lookup and personal AI subscriptions.

Create Date: 2026-11-06
Revision ID: 0062
Revises: 0061

- ``ai_tools``: the tools that report AI usage, keyed by slug, seeded with
  ``claude-code``, ``codex``, ``gemini-cli``, ``cursor``, ``copilot``,
  ``opencode`` and ``other``. Slugs reporters send that are not listed are
  added on first use, inactive.
- ``ai_subscriptions``: personal flat-rate plans (one person's seat), with the
  fee as an amount plus currency per month or year, and the days it runs
  (UTC days, both inclusive).

The instance setting ``ai_subscriptions_self_service`` is not written: when it
is missing, people may edit their own subscriptions.

The downgrade drops both tables and their rows.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0062"
down_revision = "0061"
branch_labels = None
depends_on = None

_PROVIDER_CHECK = "~ '^[a-z0-9][a-z0-9_.-]{0,49}$'"

_SEEDED_TOOLS = (
    ("claude-code", "Claude Code", "anthropic", 10),
    ("codex", "Codex", "openai", 20),
    ("gemini-cli", "Gemini CLI", "gemini", 30),
    ("cursor", "Cursor", None, 40),
    ("copilot", "GitHub Copilot", None, 50),
    ("opencode", "OpenCode", None, 60),
    ("other", "Other", None, 1000),
)


def upgrade() -> None:
    tools = op.create_table(
        "ai_tools",
        sa.Column("slug", sa.String(50), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("default_provider", sa.String(50), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("position", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("slug ~ '^[a-z0-9][a-z0-9-]{1,49}$'", name="ck_ai_tools_slug_format"),
        sa.CheckConstraint(
            f"default_provider IS NULL OR default_provider {_PROVIDER_CHECK}",
            name="ck_ai_tools_default_provider_format",
        ),
        sa.CheckConstraint("btrim(name) <> ''", name="ck_ai_tools_name_not_blank"),
    )
    op.bulk_insert(
        tools,
        [
            {"slug": slug, "name": name, "default_provider": provider, "is_active": True, "position": position}
            for slug, name, provider, position in _SEEDED_TOOLS
        ],
    )

    op.create_table(
        "ai_subscriptions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", name="ai_subscriptions_user_id_fkey", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("plan_key", sa.String(50), nullable=False),
        sa.Column("plan_name", sa.String(100), nullable=False),
        sa.Column("tool_slugs", postgresql.ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'::text[]")),
        sa.Column("amount", sa.Numeric(20, 8), nullable=False),
        sa.Column(
            "currency_code",
            sa.CHAR(3),
            sa.ForeignKey("currencies.code", name="ai_subscriptions_currency_code_fkey", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("billing_period", sa.String(5), nullable=False),
        sa.Column("starts_on", sa.Date(), nullable=False),
        sa.Column("ends_on", sa.Date(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_by_id",
            sa.Integer(),
            sa.ForeignKey("users.id", name="ai_subscriptions_created_by_id_fkey", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("billing_period IN ('month', 'year')", name="ck_ai_subscriptions_billing_period"),
        sa.CheckConstraint("amount > 0", name="ck_ai_subscriptions_amount_positive"),
        sa.CheckConstraint("ends_on IS NULL OR ends_on >= starts_on", name="ck_ai_subscriptions_period"),
        sa.CheckConstraint(f"provider {_PROVIDER_CHECK}", name="ck_ai_subscriptions_provider_format"),
        sa.CheckConstraint("plan_key ~ '^[a-z0-9][a-z0-9-]{0,49}$'", name="ck_ai_subscriptions_plan_key_format"),
        sa.CheckConstraint("btrim(plan_name) <> ''", name="ck_ai_subscriptions_plan_name_not_blank"),
    )
    op.create_index("ix_ai_subscriptions_user_starts", "ai_subscriptions", ["user_id", "starts_on"])
    op.create_index("ix_ai_subscriptions_currency_code", "ai_subscriptions", ["currency_code"])
    op.create_index("ix_ai_subscriptions_created_by_id", "ai_subscriptions", ["created_by_id"])


def downgrade() -> None:
    op.drop_table("ai_subscriptions")
    op.drop_table("ai_tools")
