"""Reaction and Mention models for journal interactions."""

from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from specivo.models.base import Base, TimestampMixin


class Reaction(Base, TimestampMixin):
    """An emoji reaction on a journal entry.

    Each user can react with a given emoji only once per journal.
    """

    __tablename__ = "reactions"

    __table_args__ = (UniqueConstraint("journal_id", "user_id", "emoji", name="uq_reaction"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    journal_id: Mapped[int] = mapped_column(
        ForeignKey("journals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    emoji: Mapped[str] = mapped_column(String(50), nullable=False)

    # Relationships
    user = relationship("User", lazy="raise")

    def __repr__(self) -> str:
        return f"<Reaction id={self.id} journal_id={self.journal_id} user_id={self.user_id} emoji={self.emoji!r}>"


class Mention(Base, TimestampMixin):
    """A record that a user was @mentioned in a journal entry.

    Created when a comment containing @username is posted.
    """

    __tablename__ = "mentions"

    __table_args__ = (UniqueConstraint("journal_id", "user_id", name="uq_mention"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    journal_id: Mapped[int] = mapped_column(
        ForeignKey("journals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    def __repr__(self) -> str:
        return f"<Mention id={self.id} journal_id={self.journal_id} user_id={self.user_id}>"
