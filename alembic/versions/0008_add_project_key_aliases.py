"""Add project_key_aliases table for key rename redirects.

Revision ID: 0008
Revises: 0007
Create Date: 2026-04-05
"""

import sqlalchemy as sa

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_key_aliases",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("old_key", sa.String(10), nullable=False, unique=True),
        sa.Column("project_id", sa.Integer, sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("renamed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("renamed_by_id", sa.Integer, sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
    )
    op.create_index("ix_project_key_aliases_project_id", "project_key_aliases", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_project_key_aliases_project_id", "project_key_aliases")
    op.drop_table("project_key_aliases")
