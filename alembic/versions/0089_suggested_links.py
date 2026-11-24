"""Suggested links: document vectors, suggested pairs and the typed link kind.

Create Date: 2026-11-24
Revision ID: 0089
Revises: 0088

- ``entity_vectors``: one row per search source (an issue or a wiki page) and
  embedding model, holding the mean of the source's chunk embeddings,
  L2-normalised (untyped ``vector``, like ``chunk_embeddings``). Unique on
  ``(search_source_id, model_id)``; both foreign keys cascade.
- ``entity_suggestions``: a suggested link between two items of a project, once
  per pair and model, with typed foreign keys to ``issues`` / ``wiki_pages``
  (exactly one per end, CHECKed, ``ON DELETE CASCADE``), the cosine ``score``,
  ``status`` (``suggested``, ``accepted``, ``dismissed``), who decided and when
  (``decided_by_id`` ``ON DELETE SET NULL``). Unique on the model and the four
  item columns with ``NULLS NOT DISTINCT``. Every foreign key has its index.
- ``entity_links.link_type``: set only on the new ``typed_link`` kind, one of
  ``documents``, ``implements``, ``decided_in``, ``supersedes``, ``see_also``
  (CHECK ``ck_entity_links_link_type``).

No data to move: vectors and suggestions are filled by the nightly sweep (or
``python -m specivo.cli.similar_links``) once the upgrade is running.
"""

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

from alembic import op

revision = "0089"
down_revision = "0088"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("entity_links", sa.Column("link_type", sa.String(20), nullable=True))
    op.create_check_constraint(
        "ck_entity_links_link_type",
        "entity_links",
        "(kind = 'typed_link') = (link_type IS NOT NULL)"
        " AND (link_type IS NULL OR link_type IN"
        " ('documents', 'implements', 'decided_in', 'supersedes', 'see_also'))",
    )

    op.create_table(
        "entity_vectors",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "search_source_id",
            sa.Integer(),
            sa.ForeignKey("search_sources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "model_id",
            sa.Integer(),
            sa.ForeignKey("embedding_models.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("vector", Vector(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("uq_entity_vectors_source_model", "entity_vectors", ["search_source_id", "model_id"], unique=True)
    op.create_index("ix_entity_vectors_model_id", "entity_vectors", ["model_id"])

    op.create_table(
        "entity_suggestions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "model_id",
            sa.Integer(),
            sa.ForeignKey("embedding_models.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_issue_id", sa.Integer(), sa.ForeignKey("issues.id", ondelete="CASCADE"), nullable=True),
        sa.Column(
            "source_wiki_page_id",
            sa.Integer(),
            sa.ForeignKey("wiki_pages.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("target_issue_id", sa.Integer(), sa.ForeignKey("issues.id", ondelete="CASCADE"), nullable=True),
        sa.Column(
            "target_wiki_page_id",
            sa.Integer(),
            sa.ForeignKey("wiki_pages.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="suggested"),
        sa.Column("decided_by_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "(source_issue_id IS NOT NULL)::int + (source_wiki_page_id IS NOT NULL)::int = 1",
            name="ck_entity_suggestions_one_source",
        ),
        sa.CheckConstraint(
            "(target_issue_id IS NOT NULL)::int + (target_wiki_page_id IS NOT NULL)::int = 1",
            name="ck_entity_suggestions_one_target",
        ),
        sa.CheckConstraint("status IN ('suggested', 'accepted', 'dismissed')", name="ck_entity_suggestions_status"),
        sa.CheckConstraint("score >= -1 AND score <= 1", name="ck_entity_suggestions_score"),
    )
    op.create_index(
        "uq_entity_suggestions_pair",
        "entity_suggestions",
        ["model_id", "source_issue_id", "source_wiki_page_id", "target_issue_id", "target_wiki_page_id"],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )
    op.create_index("ix_entity_suggestions_project_status", "entity_suggestions", ["project_id", "model_id", "status"])
    for column in (
        "source_issue_id",
        "source_wiki_page_id",
        "target_issue_id",
        "target_wiki_page_id",
        "model_id",
        "decided_by_id",
    ):
        op.create_index(f"ix_entity_suggestions_{column}", "entity_suggestions", [column])


def downgrade() -> None:
    op.drop_table("entity_suggestions")
    op.drop_table("entity_vectors")
    op.execute("DELETE FROM entity_links WHERE kind = 'typed_link'")
    op.drop_constraint("ck_entity_links_link_type", "entity_links", type_="check")
    op.drop_column("entity_links", "link_type")
