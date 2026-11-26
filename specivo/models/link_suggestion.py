"""Suggested links: one document vector per issue and wiki page, and the pairs they suggest.

``entity_vectors`` holds, per search source (an issue or a wiki page) and
embedding model, the mean of the source's chunk embeddings, L2-normalised. It
is derived data: recomputed whenever the source's chunks are embedded again
(``link_suggestion_service.refresh_vectors``) and dropped with its source.

``entity_suggestions`` holds the pairs whose vectors are close: an issue or
page and its nearest neighbours in the same project, scored by cosine
similarity. A pair is stored once, in a fixed order (``pair_key``), with typed
foreign keys like ``entity_links``, so deleting either item deletes the row.
A pair someone dismissed keeps its row (``status = 'dismissed'``) and is never
suggested again for that model; an accepted one becomes a real link.
"""

from __future__ import annotations

from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from specivo.models.base import Base

STATUS_SUGGESTED = "suggested"
STATUS_ACCEPTED = "accepted"
STATUS_DISMISSED = "dismissed"
SUGGESTION_STATUSES = (STATUS_SUGGESTED, STATUS_ACCEPTED, STATUS_DISMISSED)

ITEM_ISSUE = "issue"
ITEM_WIKI_PAGE = "wiki_page"
ITEM_TYPES = (ITEM_ISSUE, ITEM_WIKI_PAGE)


def pair_key(a: tuple[str, int], b: tuple[str, int]) -> tuple[tuple[str, int], tuple[str, int]]:
    """The stored order of a pair of items: ``("issue", id)`` before ``("wiki_page", id)``, then by id."""
    return (a, b) if a <= b else (b, a)


class EntityVector(Base):
    """The document vector of one issue or wiki page for one embedding model."""

    __tablename__ = "entity_vectors"
    __table_args__ = (
        Index("uq_entity_vectors_source_model", "search_source_id", "model_id", unique=True),
        Index("ix_entity_vectors_model_id", "model_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    search_source_id: Mapped[int] = mapped_column(ForeignKey("search_sources.id", ondelete="CASCADE"), nullable=False)
    model_id: Mapped[int] = mapped_column(ForeignKey("embedding_models.id", ondelete="CASCADE"), nullable=False)
    vector = mapped_column(Vector(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def __repr__(self) -> str:
        return f"<EntityVector source={self.search_source_id} model={self.model_id}>"


class EntitySuggestion(Base):
    """A suggested link between two items of a project, and what was decided about it."""

    __tablename__ = "entity_suggestions"
    __table_args__ = (
        CheckConstraint(
            "(source_issue_id IS NOT NULL)::int + (source_wiki_page_id IS NOT NULL)::int = 1",
            name="ck_entity_suggestions_one_source",
        ),
        CheckConstraint(
            "(target_issue_id IS NOT NULL)::int + (target_wiki_page_id IS NOT NULL)::int = 1",
            name="ck_entity_suggestions_one_target",
        ),
        CheckConstraint(
            "status IN ('suggested', 'accepted', 'dismissed')",
            name="ck_entity_suggestions_status",
        ),
        CheckConstraint("score >= -1 AND score <= 1", name="ck_entity_suggestions_score"),
        Index(
            "uq_entity_suggestions_pair",
            "model_id",
            "source_issue_id",
            "source_wiki_page_id",
            "target_issue_id",
            "target_wiki_page_id",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_entity_suggestions_project_status", "project_id", "model_id", "status"),
        Index("ix_entity_suggestions_source_issue_id", "source_issue_id"),
        Index("ix_entity_suggestions_source_wiki_page_id", "source_wiki_page_id"),
        Index("ix_entity_suggestions_target_issue_id", "target_issue_id"),
        Index("ix_entity_suggestions_target_wiki_page_id", "target_wiki_page_id"),
        Index("ix_entity_suggestions_model_id", "model_id"),
        Index("ix_entity_suggestions_decided_by_id", "decided_by_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    model_id: Mapped[int] = mapped_column(ForeignKey("embedding_models.id", ondelete="CASCADE"), nullable=False)
    source_issue_id: Mapped[int | None] = mapped_column(ForeignKey("issues.id", ondelete="CASCADE"), nullable=True)
    source_wiki_page_id: Mapped[int | None] = mapped_column(
        ForeignKey("wiki_pages.id", ondelete="CASCADE"), nullable=True
    )
    target_issue_id: Mapped[int | None] = mapped_column(ForeignKey("issues.id", ondelete="CASCADE"), nullable=True)
    target_wiki_page_id: Mapped[int | None] = mapped_column(
        ForeignKey("wiki_pages.id", ondelete="CASCADE"), nullable=True
    )
    score: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=STATUS_SUGGESTED)
    decided_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    @property
    def source(self) -> tuple[str, int]:
        if self.source_issue_id is not None:
            return (ITEM_ISSUE, self.source_issue_id)
        return (ITEM_WIKI_PAGE, int(self.source_wiki_page_id or 0))

    @property
    def target(self) -> tuple[str, int]:
        if self.target_issue_id is not None:
            return (ITEM_ISSUE, self.target_issue_id)
        return (ITEM_WIKI_PAGE, int(self.target_wiki_page_id or 0))

    def __repr__(self) -> str:
        return f"<EntitySuggestion {self.source} ~ {self.target} {self.score:.3f} {self.status}>"
