"""add journal_emoji composite index on reactions

Revision ID: 0003
Revises: 0002
Create Date: 2026-03-29 10:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_reactions_journal_emoji", "reactions", ["journal_id", "emoji"])


def downgrade() -> None:
    op.drop_index("ix_reactions_journal_emoji", table_name="reactions")
