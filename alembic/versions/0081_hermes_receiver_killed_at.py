"""Hermes receivers record when the kill switch stopped them.

Create Date: 2026-11-23
Revision ID: 0081
Revises: 0080

The kill switch stops an agent's API keys; it now stops its Hermes event
receivers too. ``hermes_receivers.killed_at`` (nullable) is set by
``kill_agent`` / ``kill_all`` and cleared by ``unkill_agent``. It is kept apart
from ``is_active`` (the owner's switch), so lifting a kill never turns on a
receiver its owner had switched off. Existing receivers start not killed.
"""

import sqlalchemy as sa

from alembic import op

revision = "0081"
down_revision = "0080"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("hermes_receivers", sa.Column("killed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("hermes_receivers", "killed_at")
