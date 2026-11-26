"""Concepts an LLM extracted from a project's issues and wiki pages, and where they are mentioned.

``entity_concepts``: one row per project and normalised name (``normalized``,
see ``concept_extraction.normalize_name``), so the same feature written two
ways is one concept. ``kind`` is one of :data:`CONCEPT_KINDS`.

``concept_mentions``: one row per concept and source (an issue or a wiki
page), with the passage of the source that mentions it (a literal excerpt,
checked against the text sent), its span in that text and the model's
confidence. Typed foreign keys like ``entity_links``, so deleting the source
deletes its mentions.

``concept_extraction_sources``: per source, the hash of what was last sent to
the model, so only changed sources are extracted again.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from specivo.models.base import Base

KIND_FEATURE = "feature"
KIND_EXTERNAL_SYSTEM = "external_system"
KIND_DECISION = "decision"
KIND_TERM = "term"
CONCEPT_KINDS: tuple[str, ...] = (KIND_FEATURE, KIND_EXTERNAL_SYSTEM, KIND_DECISION, KIND_TERM)

SOURCE_STATUS_DONE = "done"
SOURCE_STATUS_REJECTED = "rejected"
SOURCE_STATUS_FAILED = "failed"
SOURCE_STATUSES: tuple[str, ...] = (SOURCE_STATUS_DONE, SOURCE_STATUS_REJECTED, SOURCE_STATUS_FAILED)

_ONE_SOURCE = "(source_issue_id IS NOT NULL)::int + (source_wiki_page_id IS NOT NULL)::int = 1"


class EntityConcept(Base):
    """A concept of a project: a feature, an external system, a decision or a term."""

    __tablename__ = "entity_concepts"
    __table_args__ = (
        CheckConstraint("kind IN ('feature', 'external_system', 'decision', 'term')", name="ck_entity_concepts_kind"),
        Index("uq_entity_concepts_project_normalized", "project_id", "normalized", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    normalized: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def __repr__(self) -> str:
        return f"<EntityConcept {self.project_id}/{self.normalized!r} {self.kind}>"


class ConceptMention(Base):
    """Where a concept is mentioned: the source, the passage, its span and the model's confidence."""

    __tablename__ = "concept_mentions"
    __table_args__ = (
        CheckConstraint(_ONE_SOURCE, name="ck_concept_mentions_one_source"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_concept_mentions_confidence"),
        CheckConstraint("span_start >= 0 AND span_end >= span_start", name="ck_concept_mentions_span"),
        Index(
            "uq_concept_mentions_concept_source",
            "concept_id",
            "source_issue_id",
            "source_wiki_page_id",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_concept_mentions_source_issue_id", "source_issue_id"),
        Index("ix_concept_mentions_source_wiki_page_id", "source_wiki_page_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    concept_id: Mapped[int] = mapped_column(ForeignKey("entity_concepts.id", ondelete="CASCADE"), nullable=False)
    source_issue_id: Mapped[int | None] = mapped_column(ForeignKey("issues.id", ondelete="CASCADE"), nullable=True)
    source_wiki_page_id: Mapped[int | None] = mapped_column(
        ForeignKey("wiki_pages.id", ondelete="CASCADE"), nullable=True
    )
    passage: Mapped[str] = mapped_column(Text, nullable=False)
    span_start: Mapped[int] = mapped_column(Integer, nullable=False)
    span_end: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    @property
    def source(self) -> tuple[str, int]:
        if self.source_issue_id is not None:
            return ("issue", self.source_issue_id)
        return ("wiki_page", int(self.source_wiki_page_id or 0))

    def __repr__(self) -> str:
        return f"<ConceptMention concept={self.concept_id} {self.source} {self.confidence:.2f}>"


class ConceptExtractionSource(Base):
    """What was last sent to the model for one source: its hash, when, and what came of it."""

    __tablename__ = "concept_extraction_sources"
    __table_args__ = (
        CheckConstraint(_ONE_SOURCE, name="ck_concept_extraction_sources_one_source"),
        CheckConstraint("status IN ('done', 'rejected', 'failed')", name="ck_concept_extraction_sources_status"),
        CheckConstraint("failures >= 0", name="ck_concept_extraction_sources_failures"),
        Index(
            "uq_concept_extraction_sources_source",
            "source_issue_id",
            "source_wiki_page_id",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_concept_extraction_sources_project_id", "project_id"),
        Index("ix_concept_extraction_sources_source_wiki_page_id", "source_wiki_page_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    source_issue_id: Mapped[int | None] = mapped_column(ForeignKey("issues.id", ondelete="CASCADE"), nullable=True)
    source_wiki_page_id: Mapped[int | None] = mapped_column(
        ForeignKey("wiki_pages.id", ondelete="CASCADE"), nullable=True
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False)
    # Failed calls for this content (status ``failed``); reset by a usable or rejected answer.
    failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    extracted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def __repr__(self) -> str:
        return f"<ConceptExtractionSource project={self.project_id} {self.status}>"
