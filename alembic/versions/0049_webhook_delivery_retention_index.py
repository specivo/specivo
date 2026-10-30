"""Partial index for pruning the outgoing webhook delivery log.

Create Date: 2026-10-30
Revision ID: 0049
Revises: 0048

The scheduled cleanup deletes settled deliveries (``succeeded``, ``failed``)
older than the retention period, oldest first, in batches. Without an index
on ``created_at`` each batch, and in particular the last one that finds
nothing more to delete, scans the whole table. Pending rows are left out:
they are never pruned.
"""

import sqlalchemy as sa

from alembic import op

revision = "0049"
down_revision = "0048"


def upgrade() -> None:
    op.create_index(
        "ix_webhook_deliveries_settled_created_at",
        "webhook_deliveries",
        ["created_at"],
        postgresql_where=sa.text("status IN ('succeeded', 'failed')"),
    )


def downgrade() -> None:
    op.drop_index("ix_webhook_deliveries_settled_created_at", table_name="webhook_deliveries")
