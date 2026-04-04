"""add project color field

Revision ID: 0004
Revises: 0003
Create Date: 2026-03-29 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("color", sa.String(7), nullable=True))


def downgrade() -> None:
    op.drop_column("projects", "color")
