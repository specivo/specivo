"""Add tags and tag_links tables.

Create Date: 2026-06-19
Revision ID: 0024
Revises: 0023

Introduces per-project tags (case-insensitively unique by name) and a
typed-FK association table linking a tag to exactly one target entity —
an issue or a wiki page.
"""

import sqlalchemy as sa

from alembic import op

revision = "0024"
down_revision = "0023"


def upgrade() -> None:
    op.create_table(
        "tags",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Integer(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("color", sa.String(7), nullable=True),
        sa.Column(
            "created_by_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_tags_project_id", "tags", ["project_id"])
    op.create_index(
        "uq_tags_project_name_lower",
        "tags",
        ["project_id", sa.text("lower(name)")],
        unique=True,
    )

    op.create_table(
        "tag_links",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "tag_id",
            sa.Integer(),
            sa.ForeignKey("tags.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "issue_id",
            sa.Integer(),
            sa.ForeignKey("issues.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "wiki_page_id",
            sa.Integer(),
            sa.ForeignKey("wiki_pages.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "created_by_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "(issue_id IS NOT NULL)::int + (wiki_page_id IS NOT NULL)::int = 1",
            name="ck_tag_links_one_target",
        ),
        sa.UniqueConstraint("tag_id", "issue_id", name="uq_tag_links_tag_issue"),
        sa.UniqueConstraint("tag_id", "wiki_page_id", name="uq_tag_links_tag_wiki_page"),
    )
    op.create_index("ix_tag_links_tag_id", "tag_links", ["tag_id"])
    op.create_index("ix_tag_links_issue_id", "tag_links", ["issue_id"])
    op.create_index("ix_tag_links_wiki_page_id", "tag_links", ["wiki_page_id"])


def downgrade() -> None:
    op.drop_table("tag_links")
    op.drop_table("tags")
