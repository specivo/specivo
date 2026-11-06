"""Move billing_rates onto the money convention.

Create Date: 2026-11-06
Revision ID: 0060
Revises: 0059

``currency`` (a free ``VARCHAR(3)``, default ``USD``) becomes ``currency_code``,
a ``CHAR(3)`` referencing ``currencies.code`` with its own index, and
``hourly_rate`` widens from ``NUMERIC(10, 2)`` to the computed amount scale
``NUMERIC(20, 8)``, so 0- and 3-decimal currencies fit. Existing codes are
trimmed and upper-cased; a code that is not in ``currencies`` stops the
upgrade with the offending values, so it can be fixed by hand first.

The downgrade restores the old columns. It refuses to run when a rate would
not fit ``NUMERIC(10, 2)`` exactly (more than two decimal places, or too
large), because narrowing would round it silently.
"""

import sqlalchemy as sa

from alembic import op

revision = "0060"
down_revision = "0059"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    unknown = bind.execute(
        sa.text(
            "SELECT coalesce(b.currency, '(null)') AS code, count(*) FROM billing_rates b "
            "LEFT JOIN currencies c ON c.code = upper(trim(b.currency)) "
            "WHERE c.code IS NULL GROUP BY b.currency ORDER BY 1"
        )
    ).all()
    if unknown:
        listed = ", ".join(f"{code!r} ({count} rate{'s' if count != 1 else ''})" for code, count in unknown)
        raise RuntimeError(
            "billing_rates holds currency codes that are not ISO 4217 codes in the currencies table: "
            f"{listed}. Change them to a valid code (for example UPDATE billing_rates SET currency = 'USD' "
            "WHERE currency = '...') and run the migration again."
        )

    op.add_column("billing_rates", sa.Column("currency_code", sa.CHAR(3), nullable=True))
    op.execute("UPDATE billing_rates SET currency_code = upper(trim(currency))")
    op.alter_column("billing_rates", "currency_code", nullable=False)
    op.create_foreign_key(
        "billing_rates_currency_code_fkey",
        "billing_rates",
        "currencies",
        ["currency_code"],
        ["code"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_billing_rates_currency_code", "billing_rates", ["currency_code"])
    op.drop_column("billing_rates", "currency")
    op.alter_column(
        "billing_rates",
        "hourly_rate",
        type_=sa.Numeric(20, 8),
        existing_type=sa.Numeric(10, 2),
        existing_nullable=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    lossy = bind.execute(
        sa.text(
            "SELECT id, hourly_rate FROM billing_rates "
            "WHERE hourly_rate <> round(hourly_rate, 2) OR abs(hourly_rate) >= 100000000 ORDER BY id"
        )
    ).all()
    if lossy:
        listed = ", ".join(f"id {row_id}: {rate}" for row_id, rate in lossy)
        raise RuntimeError(
            "Cannot downgrade billing_rates: these hourly rates do not fit NUMERIC(10, 2) without rounding: "
            f"{listed}. Change them first."
        )

    op.alter_column(
        "billing_rates",
        "hourly_rate",
        type_=sa.Numeric(10, 2),
        existing_type=sa.Numeric(20, 8),
        existing_nullable=False,
    )
    op.add_column(
        "billing_rates",
        sa.Column("currency", sa.String(3), nullable=False, server_default="USD"),
    )
    op.execute("UPDATE billing_rates SET currency = currency_code")
    op.drop_index("ix_billing_rates_currency_code", table_name="billing_rates")
    op.drop_constraint("billing_rates_currency_code_fkey", "billing_rates", type_="foreignkey")
    op.drop_column("billing_rates", "currency_code")
