"""Add the ISO 4217 currencies table, the reporting currency setting and the project override.

Create Date: 2026-11-06
Revision ID: 0056
Revises: 0055

``currencies`` is seeded from the committed ``specivo/data/iso4217.csv``:
national currencies active, funds, metals, test and special codes inactive.
The instance setting ``reporting_currency`` starts as USD (an existing value is
kept), and ``projects.reporting_currency_code`` is an optional per-project
override of it.
"""

import sqlalchemy as sa

from alembic import op

revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from specivo.core.currency_data import DEFAULT_REPORTING_CURRENCY, load_iso4217

    currencies = op.create_table(
        "currencies",
        sa.Column("code", sa.CHAR(3), primary_key=True),
        sa.Column("numeric_code", sa.SmallInteger(), nullable=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("minor_units", sa.SmallInteger(), nullable=True),
        sa.Column("symbol", sa.String(8), nullable=True),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("numeric_code", name="currencies_numeric_code_key"),
        sa.CheckConstraint("code ~ '^[A-Z]{3}$'", name="ck_currencies_code_format"),
        sa.CheckConstraint(
            "kind IN ('national', 'fund', 'metal', 'special', 'test')",
            name="ck_currencies_kind",
        ),
        sa.CheckConstraint(
            "minor_units IS NULL OR minor_units BETWEEN 0 AND 8",
            name="ck_currencies_minor_units",
        ),
    )
    assert currencies is not None
    op.bulk_insert(
        currencies,
        [
            {
                "code": c.code,
                "numeric_code": c.numeric_code,
                "name": c.name,
                "minor_units": c.minor_units,
                "symbol": c.symbol,
                "kind": c.kind,
                "is_active": c.is_active,
            }
            for c in load_iso4217()
        ],
    )

    op.add_column(
        "projects",
        sa.Column(
            "reporting_currency_code",
            sa.CHAR(3),
            sa.ForeignKey("currencies.code", name="projects_reporting_currency_code_fkey", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    op.create_index("ix_projects_reporting_currency_code", "projects", ["reporting_currency_code"])

    op.execute(
        sa.text(
            "INSERT INTO settings (key, value, created_at, updated_at) "
            "VALUES ('reporting_currency', :code, now(), now()) ON CONFLICT (key) DO NOTHING"
        ).bindparams(code=DEFAULT_REPORTING_CURRENCY)
    )


def downgrade() -> None:
    op.execute("DELETE FROM settings WHERE key = 'reporting_currency'")
    op.drop_index("ix_projects_reporting_currency_code", table_name="projects")
    op.drop_column("projects", "reporting_currency_code")
    op.drop_table("currencies")
