"""Add metadata_presets table and preset_slug column to metadata_schemas.

Revision ID: 0010
Revises: 0009
Create Date: 2026-04-05
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "metadata_presets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("slug", sa.String(100), nullable=False, unique=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("icon", sa.String(50), nullable=False, server_default="default"),
        sa.Column("schema_definition", postgresql.JSONB(), nullable=False),
        sa.Column("is_builtin", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_metadata_presets_slug", "metadata_presets", ["slug"], unique=True)

    op.add_column(
        "metadata_schemas",
        sa.Column("preset_slug", sa.String(100), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("metadata_schemas", "preset_slug")
    op.drop_index("ix_metadata_presets_slug", table_name="metadata_presets")
    op.drop_table("metadata_presets")
