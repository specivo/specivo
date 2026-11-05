"""Store the links written in issue descriptions, comments and wiki pages.

Create Date: 2026-11-05
Revision ID: 0054
Revises: 0053

``entity_links`` replaces ``wiki_page_links``, which held only ``[[Page]]``
links between pages of one wiki. Those rows are carried over; links in issue
descriptions and comments are not parsed here (that needs the Markdown
renderer), so run ``python -m specivo.cli.rebuild_links`` (``make
rebuild-links``) once after upgrading to fill them in.
"""

import sqlalchemy as sa
from alembic import op

revision = "0054"
down_revision = "0053"

_UNRESOLVED = "target_issue_id IS NULL AND target_wiki_page_id IS NULL AND target_attachment_id IS NULL"

COPY_WIKI_LINKS_SQL = """
    INSERT INTO entity_links (
        source_type, source_wiki_page_id, source_project_id, kind, target_type,
        target_wiki_page_id, target_project_id, target_ref, display_text, created_at
    )
    SELECT 'wiki_page', l.source_page_id, w.project_id, 'wiki_link', 'wiki_page',
           l.target_page_id, w.project_id, l.target_slug, l.display_text, l.created_at
    FROM wiki_page_links l
    JOIN wikis w ON w.id = l.wiki_id
    ON CONFLICT DO NOTHING
"""

COPY_BACK_SQL = """
    INSERT INTO wiki_page_links (wiki_id, source_page_id, target_page_id, target_slug, display_text, created_at)
    SELECT p.wiki_id, l.source_wiki_page_id, l.target_wiki_page_id, l.target_ref, l.display_text, l.created_at
    FROM entity_links l
    JOIN wiki_pages p ON p.id = l.source_wiki_page_id
    WHERE l.kind = 'wiki_link'
    ON CONFLICT DO NOTHING
"""


def upgrade() -> None:
    op.create_table(
        "entity_links",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_type", sa.String(16), nullable=False),
        sa.Column("source_issue_id", sa.Integer(), sa.ForeignKey("issues.id", ondelete="CASCADE"), nullable=True),
        sa.Column(
            "source_journal_id", sa.Integer(), sa.ForeignKey("journals.id", ondelete="CASCADE"), nullable=True
        ),
        sa.Column(
            "source_wiki_page_id", sa.Integer(), sa.ForeignKey("wiki_pages.id", ondelete="CASCADE"), nullable=True
        ),
        sa.Column(
            "source_project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("target_type", sa.String(16), nullable=False),
        sa.Column("target_issue_id", sa.Integer(), sa.ForeignKey("issues.id", ondelete="SET NULL"), nullable=True),
        sa.Column(
            "target_wiki_page_id", sa.Integer(), sa.ForeignKey("wiki_pages.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column(
            "target_attachment_id",
            sa.Integer(),
            sa.ForeignKey("attachments.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "target_project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=True
        ),
        sa.Column("target_ref", sa.String(255), nullable=False),
        sa.Column("display_text", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "(source_issue_id IS NOT NULL)::int + (source_journal_id IS NOT NULL)::int"
            " + (source_wiki_page_id IS NOT NULL)::int = 1",
            name="ck_entity_links_one_source",
        ),
        sa.CheckConstraint(
            "(target_issue_id IS NOT NULL)::int + (target_wiki_page_id IS NOT NULL)::int"
            " + (target_attachment_id IS NOT NULL)::int <= 1",
            name="ck_entity_links_one_target",
        ),
    )
    op.create_index(
        "uq_entity_links_source_target",
        "entity_links",
        [
            "source_issue_id",
            "source_journal_id",
            "source_wiki_page_id",
            "kind",
            "target_type",
            "target_project_id",
            "target_ref",
        ],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )
    for column in (
        "source_issue_id",
        "source_journal_id",
        "source_wiki_page_id",
        "source_project_id",
        "target_issue_id",
        "target_wiki_page_id",
        "target_attachment_id",
        "target_project_id",
    ):
        op.create_index(f"ix_entity_links_{column}", "entity_links", [column])
    op.create_index(
        "ix_entity_links_unresolved",
        "entity_links",
        ["target_type", "target_project_id", "target_ref"],
        postgresql_where=sa.text(_UNRESOLVED),
    )

    op.execute(COPY_WIKI_LINKS_SQL)
    op.drop_table("wiki_page_links")


def downgrade() -> None:
    op.create_table(
        "wiki_page_links",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("wiki_id", sa.Integer(), sa.ForeignKey("wikis.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "source_page_id", sa.Integer(), sa.ForeignKey("wiki_pages.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "target_page_id", sa.Integer(), sa.ForeignKey("wiki_pages.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("target_slug", sa.String(255), nullable=False),
        sa.Column("display_text", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
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
    op.execute(COPY_BACK_SQL)
    op.drop_table("entity_links")
