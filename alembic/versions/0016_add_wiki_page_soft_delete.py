"""Add soft-delete columns to wiki_pages.

Create Date: 2026-04-12
Revision ID: 0016
Revises: 0015

Adds deleted_at and deleted_by_id for soft-delete support.
Replaces the full unique constraint with a partial unique index
that only enforces uniqueness for non-deleted pages.
"""

import sqlalchemy as sa

from alembic import op

revision = "0016"
down_revision = "0015"


def upgrade() -> None:
    op.add_column("wiki_pages", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "wiki_pages",
        sa.Column("deleted_by_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
    )
    op.create_index("ix_wiki_pages_deleted_at", "wiki_pages", ["deleted_at"])

    # Replace full unique constraint with partial unique index (active pages only)
    op.drop_constraint("uq_wiki_pages_wiki_slug", "wiki_pages", type_="unique")
    op.create_index(
        "uq_wiki_pages_wiki_slug_active",
        "wiki_pages",
        ["wiki_id", "slug"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_wiki_pages_wiki_slug_active", table_name="wiki_pages")
    op.create_unique_constraint("uq_wiki_pages_wiki_slug", "wiki_pages", ["wiki_id", "slug"])
    op.drop_index("ix_wiki_pages_deleted_at", table_name="wiki_pages")
    op.drop_column("wiki_pages", "deleted_by_id")
    op.drop_column("wiki_pages", "deleted_at")
