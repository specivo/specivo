"""Case-insensitive unique index on metadata_presets.slug.

Create Date: 2026-06-18
Revision ID: 0023
Revises: 0022

Adds a functional unique index on ``lower(slug)`` so preset identifiers are
unique case-insensitively at the database level, complementing the
service-layer normalization and duplicate check.
"""

import sqlalchemy as sa

from alembic import op

revision = "0023"
down_revision = "0022"


def upgrade() -> None:
    op.create_index(
        "ix_metadata_presets_slug_lower",
        "metadata_presets",
        [sa.text("lower(slug)")],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_metadata_presets_slug_lower", table_name="metadata_presets")
