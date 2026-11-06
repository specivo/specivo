"""Add the exchange_rates table.

Create Date: 2026-11-06
Revision ID: 0058
Revises: 0057

One row per day, currency pair and source (``manual``, ``csv``, ``ecb``):
``1 base_code = rate quote_code``. The settings that steer conversion
(``exchange_rate_pivot``, ``exchange_rate_max_age_days``, the optional fetch)
are read with defaults when missing, so nothing is inserted into ``settings``;
the downgrade removes any that were saved.
"""

import sqlalchemy as sa

from alembic import op

revision = "0058"
down_revision = "0057"
branch_labels = None
depends_on = None

_SETTING_KEYS = (
    "exchange_rate_pivot",
    "exchange_rate_max_age_days",
    "exchange_rates_fetch_enabled",
    "exchange_rates_fetch_url",
    "exchange_rates_last_fetch",
)


def upgrade() -> None:
    op.create_table(
        "exchange_rates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("rate_date", sa.Date(), nullable=False),
        sa.Column(
            "base_code",
            sa.CHAR(3),
            sa.ForeignKey("currencies.code", name="exchange_rates_base_code_fkey", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "quote_code",
            sa.CHAR(3),
            sa.ForeignKey("currencies.code", name="exchange_rates_quote_code_fkey", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("rate", sa.Numeric(24, 12), nullable=False),
        sa.Column("source", sa.String(10), nullable=False),
        sa.Column(
            "created_by_id",
            sa.Integer(),
            sa.ForeignKey("users.id", name="exchange_rates_created_by_id_fkey", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint(
            "rate_date", "base_code", "quote_code", "source", name="uq_exchange_rates_date_pair_source"
        ),
        sa.CheckConstraint("base_code <> quote_code", name="ck_exchange_rates_distinct_pair"),
        sa.CheckConstraint("rate > 0", name="ck_exchange_rates_rate_positive"),
        sa.CheckConstraint("source IN ('manual', 'csv', 'ecb')", name="ck_exchange_rates_source"),
    )
    op.create_index(
        "ix_exchange_rates_pair_date",
        "exchange_rates",
        ["base_code", "quote_code", sa.text("rate_date DESC")],
    )
    op.create_index("ix_exchange_rates_quote_code", "exchange_rates", ["quote_code"])
    op.create_index("ix_exchange_rates_created_by_id", "exchange_rates", ["created_by_id"])


def downgrade() -> None:
    op.get_bind().execute(
        sa.text("DELETE FROM settings WHERE key IN :keys").bindparams(sa.bindparam("keys", expanding=True)),
        {"keys": list(_SETTING_KEYS)},
    )
    op.drop_index("ix_exchange_rates_created_by_id", table_name="exchange_rates")
    op.drop_index("ix_exchange_rates_quote_code", table_name="exchange_rates")
    op.drop_index("ix_exchange_rates_pair_date", table_name="exchange_rates")
    op.drop_table("exchange_rates")
