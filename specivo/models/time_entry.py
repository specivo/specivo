"""Time tracking models: TimeEntryActivity, TimeEntry, ActiveTimer."""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from specivo.models.base import Base, TimestampMixin


class TimeEntryActivity(Base):
    """Activity category for time entries (e.g. Development, Design, Testing)."""

    __tablename__ = "time_entry_activities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")

    def __repr__(self) -> str:
        return f"<TimeEntryActivity id={self.id} name={self.name!r}>"


class TimeEntry(Base, TimestampMixin):
    """Time logged against a project or issue.

    ``hours`` uses Numeric(8,2) — NOT Float — to avoid IEEE 754 rounding.
    ``spent_on`` is the date when the work was done (not when it was logged).
    ``issue_id`` is nullable: time can be logged at project level.
    """

    __tablename__ = "time_entries"

    __table_args__ = (
        Index("ix_time_entries_project_id", "project_id"),
        Index("ix_time_entries_issue_id", "issue_id"),
        Index("ix_time_entries_user_id", "user_id"),
        Index("ix_time_entries_spent_on", "spent_on"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )

    issue_id: Mapped[int | None] = mapped_column(
        ForeignKey("issues.id", ondelete="SET NULL"),
        nullable=True,
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    activity_id: Mapped[int] = mapped_column(
        ForeignKey("time_entry_activities.id", ondelete="RESTRICT"),
        nullable=False,
    )

    hours: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False)

    comments: Mapped[str | None] = mapped_column(Text, nullable=True)

    spent_on: Mapped[date] = mapped_column(Date, nullable=False)

    is_billable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")

    # Relationships (lazy="raise" — always use selectinload explicitly)
    user = relationship("User", foreign_keys=[user_id], lazy="raise")
    activity = relationship("TimeEntryActivity", foreign_keys=[activity_id], lazy="raise")
    project = relationship("Project", foreign_keys=[project_id], lazy="raise")
    issue = relationship("Issue", foreign_keys=[issue_id], lazy="raise")

    def __repr__(self) -> str:
        return f"<TimeEntry id={self.id} hours={self.hours} user_id={self.user_id}>"


class ActiveTimer(Base, TimestampMixin):
    """Running timer — at most one per user.

    When stopped, the elapsed time is computed and a TimeEntry is created.
    The ActiveTimer row is then deleted.
    """

    __tablename__ = "active_timers"

    __table_args__ = (UniqueConstraint("user_id", name="uq_active_timers_user_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )

    issue_id: Mapped[int | None] = mapped_column(
        ForeignKey("issues.id", ondelete="SET NULL"),
        nullable=True,
    )

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    comments: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Relationships
    user = relationship("User", foreign_keys=[user_id], lazy="raise")
    project = relationship("Project", foreign_keys=[project_id], lazy="raise")
    issue = relationship("Issue", foreign_keys=[issue_id], lazy="raise")

    def __repr__(self) -> str:
        return f"<ActiveTimer id={self.id} user_id={self.user_id}>"
