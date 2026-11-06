"""Replace the agent cost tables with the ai_model_prices price history.

Create Date: 2026-11-06
Revision ID: 0061
Revises: 0060

Drops ``agent_token_logs`` (never written by a real client) and
``model_cost_configs`` (input and output prices only, no currency), and creates
``ai_model_prices``: one model's per-1M-token prices in one currency during a
half-open validity period ``[effective_from, effective_to)``.

- ``btree_gist`` is created for the exclusion constraint that keeps the active
  periods of one model from one source (``seed`` or ``admin``) from
  overlapping. It is a trusted extension, so a role with ``CREATE`` on the
  database can install it; like ``pg_trgm`` in 0052 the migration stops if it
  cannot.
- The trigger ``ai_model_prices_immutable`` refuses any UPDATE of the prices,
  the currency, the identity or ``effective_from``: a price changes by closing
  its row and inserting a new one, never in place.
- The table is seeded from the committed LiteLLM snapshot
  (``specivo/data/ai_model_prices.json``, plus the optional history file), all
  periods starting at the snapshot's ``as_of``.

The downgrade drops the table and recreates the two old tables empty; their
rows are not restored. ``btree_gist`` stays installed (other objects may use
it, and dropping it needs ownership).
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0061"
down_revision = "0060"
branch_labels = None
depends_on = None

_PRICE_COLUMNS = (
    "input",
    "output",
    "cache_write_5m",
    "cache_write_1h",
    "cache_read",
    "reasoning",
    "long_input",
    "long_output",
    "long_cache_write_5m",
    "long_cache_write_1h",
    "long_cache_read",
)
_IMMUTABLE = (
    "provider",
    "model",
    "currency_code",
    *_PRICE_COLUMNS,
    "long_context_threshold",
    "is_local",
    "baseline_model",
    "effective_from",
    "source",
)

_TRIGGER_FUNCTION = """
CREATE OR REPLACE FUNCTION ai_model_prices_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF ({changed}) THEN
        RAISE EXCEPTION 'ai_model_prices rows are never edited in place: close the period and add a new price'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'ai_model_prices_immutable';
    END IF;
    RETURN NEW;
END;
$$;
""".format(changed=" OR ".join(f"NEW.{c} IS DISTINCT FROM OLD.{c}" for c in _IMMUTABLE))


def upgrade() -> None:
    op.drop_index("ix_agent_token_logs_session_id", table_name="agent_token_logs")
    op.drop_table("agent_token_logs")
    op.drop_table("model_cost_configs")

    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    prices = op.create_table(
        "ai_model_prices",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("aliases", postgresql.ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'::text[]")),
        sa.Column(
            "currency_code",
            sa.CHAR(3),
            sa.ForeignKey("currencies.code", name="ai_model_prices_currency_code_fkey", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("input", sa.Numeric(18, 6), nullable=False),
        sa.Column("output", sa.Numeric(18, 6), nullable=False),
        sa.Column("cache_write_5m", sa.Numeric(18, 6), nullable=True),
        sa.Column("cache_write_1h", sa.Numeric(18, 6), nullable=True),
        sa.Column("cache_read", sa.Numeric(18, 6), nullable=True),
        sa.Column("reasoning", sa.Numeric(18, 6), nullable=True),
        sa.Column("long_context_threshold", sa.Integer(), nullable=True),
        sa.Column("long_input", sa.Numeric(18, 6), nullable=True),
        sa.Column("long_output", sa.Numeric(18, 6), nullable=True),
        sa.Column("long_cache_write_5m", sa.Numeric(18, 6), nullable=True),
        sa.Column("long_cache_write_1h", sa.Numeric(18, 6), nullable=True),
        sa.Column("long_cache_read", sa.Numeric(18, 6), nullable=True),
        sa.Column("is_local", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("baseline_model", sa.String(200), nullable=True),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.String(10), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "superseded_by_id",
            sa.Integer(),
            sa.ForeignKey("ai_model_prices.id", name="ai_model_prices_superseded_by_id_fkey", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_by_id",
            sa.Integer(),
            sa.ForeignKey("users.id", name="ai_model_prices_created_by_id_fkey", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("source IN ('seed', 'admin')", name="ck_ai_model_prices_source"),
        sa.CheckConstraint("effective_to IS NULL OR effective_to > effective_from", name="ck_ai_model_prices_period"),
        sa.CheckConstraint("model = lower(model) AND model <> ''", name="ck_ai_model_prices_model_normalized"),
        sa.CheckConstraint("provider ~ '^[a-z0-9][a-z0-9_.-]*$'", name="ck_ai_model_prices_provider_format"),
        sa.CheckConstraint(
            "long_context_threshold IS NULL OR long_context_threshold > 0",
            name="ck_ai_model_prices_long_context_threshold",
        ),
        sa.CheckConstraint(
            " AND ".join(f"({c} IS NULL OR {c} >= 0)" for c in _PRICE_COLUMNS),
            name="ck_ai_model_prices_prices_non_negative",
        ),
        sa.CheckConstraint(
            "long_context_threshold IS NOT NULL OR ("
            + " AND ".join(f"{c} IS NULL" for c in _PRICE_COLUMNS if c.startswith("long_"))
            + ")",
            name="ck_ai_model_prices_long_prices_need_threshold",
        ),
    )
    assert prices is not None
    op.execute(
        "ALTER TABLE ai_model_prices ADD CONSTRAINT ex_ai_model_prices_no_overlap "
        "EXCLUDE USING gist (provider WITH =, model WITH =, source WITH =, "
        "tstzrange(effective_from, effective_to, '[)') WITH &&) WHERE (is_active)"
    )
    op.create_index(
        "ix_ai_model_prices_model_from", "ai_model_prices", ["provider", "model", sa.text("effective_from DESC")]
    )
    op.create_index("ix_ai_model_prices_lookup", "ai_model_prices", ["model", sa.text("effective_from DESC")])
    op.create_index("ix_ai_model_prices_aliases", "ai_model_prices", ["aliases"], postgresql_using="gin")
    op.create_index("ix_ai_model_prices_currency_code", "ai_model_prices", ["currency_code"])
    op.create_index("ix_ai_model_prices_created_by_id", "ai_model_prices", ["created_by_id"])
    op.create_index("ix_ai_model_prices_superseded_by_id", "ai_model_prices", ["superseded_by_id"])

    op.execute(_TRIGGER_FUNCTION)
    op.execute(
        "CREATE TRIGGER ai_model_prices_immutable BEFORE UPDATE ON ai_model_prices "
        "FOR EACH ROW EXECUTE FUNCTION ai_model_prices_immutable()"
    )

    from specivo.services.ai_model_price_seed import apply_seed

    apply_seed(op.get_bind(), prices)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS ai_model_prices_immutable ON ai_model_prices")
    op.execute("DROP FUNCTION IF EXISTS ai_model_prices_immutable()")
    op.drop_table("ai_model_prices")

    op.create_table(
        "model_cost_configs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("model_name", sa.String(length=100), nullable=False),
        sa.Column("input_cost_per_1m", sa.Numeric(precision=10, scale=4), nullable=False),
        sa.Column("output_cost_per_1m", sa.Numeric(precision=10, scale=4), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "model_name", name="uq_model_cost"),
    )
    op.create_table(
        "agent_token_logs",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("model_name", sa.String(length=100), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost", sa.Numeric(precision=10, scale=6), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["agent_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_token_logs_session_id", "agent_token_logs", ["session_id"])
