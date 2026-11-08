"""AI usage entries, the AI usage permissions and the role setting ``ai_usage_visibility``.

Create Date: 2026-11-06
Revision ID: 0063
Revises: 0062

- ``ai_usage_entries``: one bucket of AI usage (session x model x UTC day)
  logged in a user's name, with the account and key that reported it, the
  token counts, and the cost snapshot (``price_id`` RESTRICT, ``priced_at``).
- ``roles.ai_usage_visibility`` (``all`` / ``own``, default ``own``).
- Seeded roles, found the way the seed finds them (custom roles by name;
  renamed or deleted roles are left alone, and a role granting ``*`` keeps
  it):

  ========== ============================================ ==========
  role       permissions added                            visibility
  ========== ============================================ ==========
  Manager    (``*`` already grants all four)              ``all``
  Developer  ``view_ai_usage``, ``log_ai_usage``          ``own``
  Reporter   ``view_ai_usage``                            ``own``
  Agent      ``view_ai_usage``, ``log_ai_usage``          ``own``
  ========== ============================================ ==========

The downgrade drops the table and the column and removes the added
permissions from those roles again.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0063"
down_revision = "0062"
branch_labels = None
depends_on = None

_TOKEN_COLUMNS = ("input", "output", "cache_write_5m", "cache_write_1h", "cache_read", "reasoning")

#: Permissions the migration adds per seeded role.
_GRANTS: dict[str, tuple[str, ...]] = {
    "Developer": ("view_ai_usage", "log_ai_usage"),
    "Reporter": ("view_ai_usage",),
    "Agent": ("view_ai_usage", "log_ai_usage"),
}


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _money(name: str) -> list[sa.Column]:
    return [
        sa.Column(f"{name}_amount", sa.Numeric(20, 8), nullable=True),
        sa.Column(
            f"{name}_currency",
            sa.CHAR(3),
            sa.ForeignKey("currencies.code", name=f"ai_usage_entries_{name}_currency_fkey", ondelete="RESTRICT"),
            nullable=True,
        ),
    ]


def upgrade() -> None:
    op.add_column(
        "roles",
        sa.Column("ai_usage_visibility", sa.String(30), nullable=False, server_default="own"),
    )
    op.create_check_constraint("ck_roles_ai_usage_visibility", "roles", "ai_usage_visibility IN ('all', 'own')")

    op.create_table(
        "ai_usage_entries",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Integer(),
            sa.ForeignKey("projects.id", name="ai_usage_entries_project_id_fkey", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "issue_id",
            sa.Integer(),
            sa.ForeignKey("issues.id", name="ai_usage_entries_issue_id_fkey", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", name="ai_usage_entries_user_id_fkey", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "reporter_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", name="ai_usage_entries_reporter_user_id_fkey", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "reporter_api_key_id",
            sa.Integer(),
            sa.ForeignKey("api_keys.id", name="ai_usage_entries_reporter_api_key_id_fkey", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("source", sa.String(10), nullable=False),
        sa.Column("attribution_method", sa.String(12), nullable=False, server_default="none"),
        sa.Column(
            "tool_slug",
            sa.String(50),
            sa.ForeignKey("ai_tools.slug", name="ai_usage_entries_tool_slug_fkey", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(50), nullable=True),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("request_count", sa.Integer(), nullable=False, server_default="0"),
        *[sa.Column(c, sa.BigInteger(), nullable=False, server_default="0") for c in _TOKEN_COLUMNS],
        sa.Column("long_context", postgresql.JSONB(), nullable=True),
        sa.Column("cost_type", sa.String(12), nullable=False, server_default="unknown"),
        sa.Column(
            "subscription_id",
            sa.Integer(),
            sa.ForeignKey("ai_subscriptions.id", name="ai_usage_entries_subscription_id_fkey", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "price_id",
            sa.Integer(),
            sa.ForeignKey("ai_model_prices.id", name="ai_usage_entries_price_id_fkey", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("priced_at", sa.DateTime(timezone=True), nullable=False),
        *_money("marginal"),
        *_money("api_equivalent"),
        *_money("reported_cost"),
        sa.Column("cost_basis", sa.String(10), nullable=False, server_default="unpriced"),
        sa.Column("external_session_id", sa.String(200), nullable=True),
        sa.Column("git_branch", sa.String(255), nullable=True),
        sa.Column("repo_hint", sa.String(255), nullable=True),
        sa.Column("client_name", sa.String(100), nullable=True),
        sa.Column("client_version", sa.String(50), nullable=True),
        sa.Column("comments", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(128), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("excluded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "excluded_by_id",
            sa.Integer(),
            sa.ForeignKey("users.id", name="ai_usage_entries_excluded_by_id_fkey", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            _in("source", ("manual", "api", "mcp", "reporter", "import")), name="ck_ai_usage_entries_source"
        ),
        sa.CheckConstraint(
            _in("attribution_method", ("explicit", "env", "mcp_session", "commit", "branch", "repo", "none")),
            name="ck_ai_usage_entries_attribution_method",
        ),
        sa.CheckConstraint(
            _in("cost_type", ("subscription", "api", "local", "free", "unknown")), name="ck_ai_usage_entries_cost_type"
        ),
        sa.CheckConstraint(
            _in("cost_basis", ("calculated", "reported", "manual", "unpriced", "partial")),
            name="ck_ai_usage_entries_cost_basis",
        ),
        sa.CheckConstraint(
            "source NOT IN ('reporter', 'import') OR idempotency_key IS NOT NULL",
            name="ck_ai_usage_entries_key_required",
        ),
        sa.CheckConstraint(
            "issue_id IS NULL OR project_id IS NOT NULL", name="ck_ai_usage_entries_issue_needs_project"
        ),
        sa.CheckConstraint(
            " AND ".join(f"{c} >= 0" for c in _TOKEN_COLUMNS) + " AND request_count >= 0",
            name="ck_ai_usage_entries_counts_non_negative",
        ),
        sa.CheckConstraint(
            "ended_at IS NULL OR started_at IS NULL OR ended_at >= started_at", name="ck_ai_usage_entries_period"
        ),
        sa.CheckConstraint(
            "subscription_id IS NULL OR cost_type = 'subscription'",
            name="ck_ai_usage_entries_subscription_cost_type",
        ),
        sa.CheckConstraint("revision >= 1", name="ck_ai_usage_entries_revision"),
        sa.CheckConstraint(
            "(marginal_amount IS NULL OR marginal_amount >= 0)"
            " AND (api_equivalent_amount IS NULL OR api_equivalent_amount >= 0)"
            " AND (reported_cost_amount IS NULL OR reported_cost_amount >= 0)",
            name="ck_ai_usage_entries_amounts_non_negative",
        ),
        *[
            sa.CheckConstraint(
                f"({name}_amount IS NULL) = ({name}_currency IS NULL)",
                name=f"ck_ai_usage_entries_{name}_money_pair",
            )
            for name in ("marginal", "api_equivalent", "reported_cost")
        ],
    )
    op.create_index(
        "uq_ai_usage_entries_user_key",
        "ai_usage_entries",
        ["user_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    for name, columns in (
        ("ix_ai_usage_entries_project_date", ["project_id", "usage_date"]),
        ("ix_ai_usage_entries_user_date", ["user_id", "usage_date"]),
        ("ix_ai_usage_entries_issue_id", ["issue_id"]),
        ("ix_ai_usage_entries_reporter_date", ["reporter_user_id", "usage_date"]),
        ("ix_ai_usage_entries_model_priced_at", ["provider", "model", "priced_at"]),
        ("ix_ai_usage_entries_priced_at", ["priced_at"]),
        ("ix_ai_usage_entries_reporter_api_key_id", ["reporter_api_key_id"]),
        ("ix_ai_usage_entries_tool_slug", ["tool_slug"]),
        ("ix_ai_usage_entries_subscription_id", ["subscription_id"]),
        ("ix_ai_usage_entries_price_id", ["price_id"]),
        ("ix_ai_usage_entries_excluded_by_id", ["excluded_by_id"]),
        ("ix_ai_usage_entries_marginal_currency", ["marginal_currency"]),
        ("ix_ai_usage_entries_api_equivalent_currency", ["api_equivalent_currency"]),
        ("ix_ai_usage_entries_reported_cost_currency", ["reported_cost_currency"]),
    ):
        op.create_index(name, "ai_usage_entries", columns)

    for role, permissions in _GRANTS.items():
        for permission in permissions:
            op.execute(
                sa.text(
                    """
                    UPDATE roles
                       SET permissions = permissions || jsonb_build_array(CAST(:permission AS text))
                     WHERE name = :role
                       AND builtin = 0
                       AND NOT (permissions @> jsonb_build_array(CAST(:permission AS text)))
                       AND NOT (permissions @> '["*"]'::jsonb)
                    """
                ).bindparams(role=role, permission=permission)
            )
    op.execute("UPDATE roles SET ai_usage_visibility = 'all' WHERE name = 'Manager' AND builtin = 0")


def downgrade() -> None:
    for role, permissions in _GRANTS.items():
        for permission in permissions:
            op.execute(
                sa.text(
                    "UPDATE roles SET permissions = permissions - CAST(:permission AS text) "
                    "WHERE name = :role AND builtin = 0"
                ).bindparams(role=role, permission=permission)
            )
    op.drop_table("ai_usage_entries")
    op.drop_constraint("ck_roles_ai_usage_visibility", "roles", type_="check")
    op.drop_column("roles", "ai_usage_visibility")
