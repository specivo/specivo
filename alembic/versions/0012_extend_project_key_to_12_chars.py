"""Extend project key columns from 10 to 12 characters.

Revision ID: 0012
Revises: 0011
Create Date: 2026-04-05
"""

import sqlalchemy as sa

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("projects", "key", type_=sa.String(12))
    op.alter_column("project_key_aliases", "old_key", type_=sa.String(12))
    op.alter_column("issues", "project_key", type_=sa.String(12))


def downgrade() -> None:
    op.alter_column("issues", "project_key", type_=sa.String(10))
    op.alter_column("project_key_aliases", "old_key", type_=sa.String(10))
    op.alter_column("projects", "key", type_=sa.String(10))
