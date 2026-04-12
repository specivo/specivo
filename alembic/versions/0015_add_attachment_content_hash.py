"""Add content_hash column to attachments.

Create Date: 2026-04-12
Revision ID: 0015
Revises: 0014

SHA-256 hex digest of file content, populated automatically on upload.
"""

import sqlalchemy as sa

from alembic import op

revision = "0015"
down_revision = "0014"


def upgrade() -> None:
    op.add_column("attachments", sa.Column("content_hash", sa.String(64), nullable=True))
    op.create_index("ix_attachments_content_hash", "attachments", ["content_hash"])


def downgrade() -> None:
    op.drop_index("ix_attachments_content_hash", table_name="attachments")
    op.drop_column("attachments", "content_hash")
