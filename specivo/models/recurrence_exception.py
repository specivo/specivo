"""RecurrenceException model — per-occurrence skip / override for a recurring pattern.

This is the iCal EXDATE / RECURRENCE-ID analog. It is a separate table (rather than
columns on Issue) because exceptions must be able to exist *before* and *without* a
materialised issue:

- ``skip``     — EXDATE: the occurrence is never generated and never counts as a
  completion. The expansion engine drops it (without consuming a ``COUNT``).
- ``override`` — RECURRENCE-ID: a single occurrence whose field values (or datetime)
  differ from the template. ``override_payload`` carries the per-field changes applied
  on top of the template at generation time.

Once an (overridden) instance is materialised, ``materialized_issue_id`` records it, so
the table doubles as the dedupe ledger for overrides.
"""

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from specivo.models.base import Base, TimestampMixin


class RecurrenceException(Base, TimestampMixin):
    """A skip or override for one scheduled occurrence of a recurring pattern."""

    __tablename__ = "recurrence_exceptions"

    __table_args__ = (
        UniqueConstraint(
            "recurring_pattern_id",
            "occurrence_at",
            name="uq_recurrence_exception",
        ),
        Index("ix_recurrence_exceptions_pattern_id", "recurring_pattern_id"),
        CheckConstraint(
            "kind IN ('skip','override')",
            name="ck_recurrence_exceptions_kind",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    recurring_pattern_id: Mapped[int] = mapped_column(
        ForeignKey("recurring_patterns.id", ondelete="CASCADE"),
        nullable=False,
    )

    # The *scheduled* occurrence this exception refers to (UTC).
    occurrence_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    kind: Mapped[str] = mapped_column(String(10), nullable=False)

    override_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    materialized_issue_id: Mapped[int | None] = mapped_column(
        ForeignKey("issues.id", ondelete="SET NULL"),
        nullable=True,
    )

    pattern = relationship("RecurringPattern", back_populates="exceptions", lazy="raise")

    def __repr__(self) -> str:
        return (
            f"<RecurrenceException id={self.id} pattern_id={self.recurring_pattern_id} "
            f"kind={self.kind!r} occurrence_at={self.occurrence_at!r}>"
        )
