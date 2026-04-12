"""Sprint model — time-boxed iteration for agile project management."""

from datetime import date

from sqlalchemy import CheckConstraint, Date, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from specivo.models.base import Base, TimestampMixin


class Sprint(Base, TimestampMixin):
    """A time-boxed iteration within a project.

    ``status`` values:
    - ``planned``   — not yet started
    - ``active``    — currently in progress (only one per project)
    - ``completed`` — finished; velocity snapshot recorded

    The partial unique index ``uq_sprints_project_active`` ensures at most
    one active sprint per project at the database level.
    """

    __tablename__ = "sprints"

    __table_args__ = (
        Index("ix_sprints_project_id", "project_id"),
        CheckConstraint(
            "status IN ('planned','active','completed')",
            name="ck_sprints_status",
        ),
        Index(
            "uq_sprints_project_active",
            "project_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)

    goal: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="planned", server_default="planned"
    )

    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    velocity_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    def __repr__(self) -> str:
        return f"<Sprint id={self.id} name={self.name!r} project_id={self.project_id} status={self.status!r}>"
