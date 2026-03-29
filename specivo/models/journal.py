"""Journal and JournalDetail models for issue/wiki change tracking and comments."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from specivo.models.base import Base, TimestampMixin


class Journal(Base, TimestampMixin):
    """A single entry in the activity stream for an issue or wiki page.

    A journal may contain:
    - Only field changes (pure audit entry, notes=None)
    - Only a comment (notes set, no details)
    - Both field changes and a comment (e.g. status change + explanation)

    Typed FKs (C3 fix)
    -------------------
    Polymorphic ``journalized_type`` + ``journalized_id`` are replaced with
    separate ``issue_id`` / ``wiki_page_id`` FK columns plus a CHECK constraint
    that enforces exactly one is non-null.  This provides real referential
    integrity (CASCADE delete) without orphan cleanup jobs.

    Sequence numbering (N6 fix)
    ---------------------------
    ``sequence`` is a per-entity counter (1, 2, 3 ...) used in EntityRef keys
    like ``ACME-15#3``.  Assigned atomically by JournalService using a MAX+1
    query inside the same transaction.

    Project denormalization (I10 fix)
    ----------------------------------
    ``project_id`` is denormalized from the parent entity to enable efficient
    project-scoped activity feed queries without a JOIN.  Set once at creation
    and never updated (exception: if an issue moves projects, a background task
    updates the denormalized value).
    """

    __tablename__ = "journals"

    __table_args__ = (
        # C3 fix: exactly one parent entity must be set
        CheckConstraint(
            "(issue_id IS NOT NULL)::int + (wiki_page_id IS NOT NULL)::int = 1",
            name="ck_journals_one_parent",
        ),
        # Per-entity sequence uniqueness
        UniqueConstraint("issue_id", "sequence", name="uq_journals_issue_sequence"),
        # BRIN index for time-ordered append-only data
        Index("idx_journals_created_at_brin", "created_at", postgresql_using="brin"),
        # Composite index for project activity feed (I10 fix)
        Index("idx_journals_project_created", "project_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # ------------------------------------------------------------------
    # Parent entity (typed FKs — exactly one must be set)
    # ------------------------------------------------------------------

    issue_id: Mapped[int | None] = mapped_column(
        ForeignKey("issues.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    # wiki_page_id FK is deferred to Phase 3 when the wiki_pages table exists.
    # The column is present now (nullable) so migrations don't need to change
    # the CHECK constraint later.
    wiki_page_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    # Denormalized project_id — set by JournalService, never updated after creation.
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # ------------------------------------------------------------------
    # Authorship
    # ------------------------------------------------------------------

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # Agent attribution — FK added once api_keys table exists (already present)
    api_key_id: Mapped[int | None] = mapped_column(
        ForeignKey("api_keys.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # ------------------------------------------------------------------
    # Content
    # ------------------------------------------------------------------

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    is_private: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")

    # Per-entity sequence number (1, 2, 3 ...) for EntityRef keys
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)

    # ------------------------------------------------------------------
    # Threading (1-level reply support)
    # ------------------------------------------------------------------

    reply_to_id: Mapped[int | None] = mapped_column(
        ForeignKey("journals.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # ------------------------------------------------------------------
    # Thread resolution (Linear-style)
    # ------------------------------------------------------------------

    is_resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    resolved_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ------------------------------------------------------------------
    # Editing
    # ------------------------------------------------------------------

    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    edited_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    # ------------------------------------------------------------------
    # Relationships (lazy="raise" — use selectinload explicitly)
    # ------------------------------------------------------------------

    user = relationship("User", foreign_keys=[user_id], lazy="raise")
    details = relationship(
        "JournalDetail",
        back_populates="journal",
        lazy="raise",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<Journal id={self.id} issue_id={self.issue_id} sequence={self.sequence} user_id={self.user_id}>"


class JournalDetail(Base):
    """One field change within a journal entry.

    For description changes: ``property="attr"``, ``prop_key="description"``,
    and BOTH ``old_value`` / ``new_value`` store the FULL text (per
    Tracker_Description_Versioning Section 3 — never truncated).

    ``property`` values:
    - ``"attr"``       — standard issue field (status_id, subject, description, …)
    - ``"cf"``         — custom field (future)
    - ``"attachment"`` — file attached or removed
    - ``"relation"``   — issue relation added or removed
    """

    __tablename__ = "journal_details"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    journal_id: Mapped[int] = mapped_column(
        ForeignKey("journals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    property: Mapped[str] = mapped_column(String(30), nullable=False)
    prop_key: Mapped[str] = mapped_column(String(255), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ------------------------------------------------------------------
    # Relationships
    # ------------------------------------------------------------------

    journal = relationship("Journal", back_populates="details", lazy="raise")

    def __repr__(self) -> str:
        return f"<JournalDetail id={self.id} journal_id={self.journal_id} prop={self.property!r} key={self.prop_key!r}>"
