"""add wiki_page_links table

Revision ID: 0002
Revises: 0001
Create Date: 2026-03-29 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "wiki_page_links",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("wiki_id", sa.Integer(), nullable=False),
        sa.Column("source_page_id", sa.Integer(), nullable=False),
        sa.Column("target_page_id", sa.Integer(), nullable=True),
        sa.Column("target_slug", sa.String(length=255), nullable=False),
        sa.Column("display_text", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["wiki_id"],
            ["wikis.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_page_id"],
            ["wiki_pages.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["target_page_id"],
            ["wiki_pages.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_page_id", "target_slug", name="uq_wiki_page_links_source_slug"),
    )
    op.create_index("ix_wiki_page_links_wiki_id", "wiki_page_links", ["wiki_id"])
    op.create_index("ix_wiki_page_links_target_page_id", "wiki_page_links", ["target_page_id"])
    op.create_index(
        "ix_wiki_page_links_broken_target",
        "wiki_page_links",
        ["wiki_id", "target_slug"],
        postgresql_where=sa.text("target_page_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_wiki_page_links_broken_target", table_name="wiki_page_links")
    op.drop_index("ix_wiki_page_links_target_page_id", table_name="wiki_page_links")
    op.drop_index("ix_wiki_page_links_wiki_id", table_name="wiki_page_links")
    op.drop_table("wiki_page_links")
