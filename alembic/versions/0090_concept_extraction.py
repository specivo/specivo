"""Concept extraction: a project's AI provider, extracted concepts, their mentions and the source hashes.

Create Date: 2026-11-24
Revision ID: 0090
Revises: 0089

- ``project_ai_providers``: at most one per project (unique ``project_id``,
  ``ON DELETE CASCADE``): an OpenAI-compatible chat completions endpoint
  (``base_url``, ``model``), the provider name used for pricing and the API
  key sealed with ``secret_box`` (nullable: local servers need none).
  ``updated_by_id`` is ``ON DELETE SET NULL``.
- ``entity_concepts``: a concept of a project, unique on ``(project_id,
  normalized)``; ``kind`` is CHECKed (feature, external_system, decision, term).
- ``concept_mentions``: one per concept and source, with typed foreign keys to
  ``issues`` / ``wiki_pages`` (exactly one, CHECKed, ``ON DELETE CASCADE``),
  the passage, its span and the confidence (0 to 1, CHECKed). Unique on the
  concept and both source columns with ``NULLS NOT DISTINCT``.
- ``concept_extraction_sources``: per source, the hash of the content last
  sent to the model and whether its answer was usable (``done``/``rejected``).

Project settings (JSONB, no migration): ``concept_extraction`` (off unless
true), ``concept_extraction_enabled_by``, ``concept_confidence_threshold``,
``concept_extraction_run_limit``. Nothing to backfill: extraction starts when
a project enables it.
"""

import sqlalchemy as sa

from alembic import op

revision = "0090"
down_revision = "0089"
branch_labels = None
depends_on = None

_ONE_SOURCE = "(source_issue_id IS NOT NULL)::int + (source_wiki_page_id IS NOT NULL)::int = 1"


def _source_columns() -> list[sa.Column]:
    return [
        sa.Column("source_issue_id", sa.Integer(), sa.ForeignKey("issues.id", ondelete="CASCADE"), nullable=True),
        sa.Column(
            "source_wiki_page_id",
            sa.Integer(),
            sa.ForeignKey("wiki_pages.id", ondelete="CASCADE"),
            nullable=True,
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "project_ai_providers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("base_url", sa.String(500), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("api_key_encrypted", sa.Text(), nullable=True),
        sa.Column("updated_by_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_project_ai_providers_project_id", "project_ai_providers", ["project_id"], unique=True)
    op.create_index("ix_project_ai_providers_updated_by_id", "project_ai_providers", ["updated_by_id"])

    op.create_table(
        "entity_concepts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("normalized", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('feature', 'external_system', 'decision', 'term')", name="ck_entity_concepts_kind"
        ),
    )
    op.create_index(
        "uq_entity_concepts_project_normalized", "entity_concepts", ["project_id", "normalized"], unique=True
    )

    op.create_table(
        "concept_mentions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("concept_id", sa.Integer(), sa.ForeignKey("entity_concepts.id", ondelete="CASCADE"), nullable=False),
        *_source_columns(),
        sa.Column("passage", sa.Text(), nullable=False),
        sa.Column("span_start", sa.Integer(), nullable=False),
        sa.Column("span_end", sa.Integer(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(_ONE_SOURCE, name="ck_concept_mentions_one_source"),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_concept_mentions_confidence"),
        sa.CheckConstraint("span_start >= 0 AND span_end >= span_start", name="ck_concept_mentions_span"),
    )
    op.create_index(
        "uq_concept_mentions_concept_source",
        "concept_mentions",
        ["concept_id", "source_issue_id", "source_wiki_page_id"],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )
    op.create_index("ix_concept_mentions_source_issue_id", "concept_mentions", ["source_issue_id"])
    op.create_index("ix_concept_mentions_source_wiki_page_id", "concept_mentions", ["source_wiki_page_id"])

    op.create_table(
        "concept_extraction_sources",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        *_source_columns(),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("extracted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(_ONE_SOURCE, name="ck_concept_extraction_sources_one_source"),
        sa.CheckConstraint("status IN ('done', 'rejected')", name="ck_concept_extraction_sources_status"),
    )
    op.create_index(
        "uq_concept_extraction_sources_source",
        "concept_extraction_sources",
        ["source_issue_id", "source_wiki_page_id"],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )
    op.create_index("ix_concept_extraction_sources_project_id", "concept_extraction_sources", ["project_id"])
    op.create_index(
        "ix_concept_extraction_sources_source_wiki_page_id", "concept_extraction_sources", ["source_wiki_page_id"]
    )


def downgrade() -> None:
    op.drop_table("concept_extraction_sources")
    op.drop_table("concept_mentions")
    op.drop_table("entity_concepts")
    op.drop_table("project_ai_providers")
