"""Watcher model — users subscribed to notifications for an entity."""

from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from specivo.models.base import Base, TimestampMixin


class Watcher(Base, TimestampMixin):
    """Tracks which users are watching a given issue or wiki page.

    Typed FKs (I8 fix)
    -------------------
    Separate ``issue_id`` / ``wiki_page_id`` FK columns replace a polymorphic
    ``watchable_type`` + ``watchable_id`` pattern.  A CHECK constraint enforces
    exactly one is non-null, giving real CASCADE delete semantics.

    ``wiki_page_id`` FK is deferred to Phase 3 when the wiki_pages table exists.
    The column is present now (nullable) so the CHECK constraint doesn't need to
    change later.
    """

    __tablename__ = "watchers"

    __table_args__ = (
        CheckConstraint(
            "(issue_id IS NOT NULL)::int + (wiki_page_id IS NOT NULL)::int = 1",
            name="ck_watchers_one_parent",
        ),
        UniqueConstraint("issue_id", "user_id", name="uq_watchers_issue_user"),
        Index("ix_watchers_issue_id", "issue_id"),
        Index("ix_watchers_user_id", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    issue_id: Mapped[int | None] = mapped_column(
        ForeignKey("issues.id", ondelete="CASCADE"),
        nullable=True,
    )

    # wiki_page_id FK added Phase 3
    wiki_page_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    # ------------------------------------------------------------------
    # Relationships
    # ------------------------------------------------------------------

    user = relationship("User", foreign_keys=[user_id], lazy="raise")

    def __repr__(self) -> str:
        return f"<Watcher id={self.id} issue_id={self.issue_id} user_id={self.user_id}>"
