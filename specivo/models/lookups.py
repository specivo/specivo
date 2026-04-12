"""Lookup / configuration models: IssueStatus, Tracker, IssuePriority, IssueCategory."""

import enum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from specivo.models.base import Base


class StatusCategory(enum.StrEnum):
    """Workflow category for issue statuses."""

    backlog = "backlog"
    active = "active"
    done = "done"
    closed = "closed"


class IssueStatus(Base):
    """Available statuses for issues (e.g. New, In Progress, Closed).

    Each status belongs to a ``category`` that controls how it behaves in
    filters and progress calculations:

    - **backlog** — not started (open in filters, not counted in progress)
    - **active** — work in progress (open in filters, not counted in progress)
    - **done** — completed (open in filters, counted in progress)
    - **closed** — terminal (excluded from "open" filters, counted in progress)
    """

    __tablename__ = "issue_statuses"
    __table_args__ = (
        CheckConstraint(
            "category IN ('backlog', 'active', 'done', 'closed')",
            name="ck_issue_statuses_category",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    name: Mapped[str] = mapped_column(String(255), nullable=False)

    category: Mapped[str] = mapped_column(String(20), nullable=False, default="backlog", server_default="backlog")

    # Display ordering
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    # Optional default done-ratio when this status is set
    default_done_ratio: Mapped[int | None] = mapped_column(Integer, nullable=True)

    @property
    def is_closed(self) -> bool:
        """Convenience property: True only for terminal (closed) statuses."""
        return self.category == StatusCategory.closed

    @property
    def is_done(self) -> bool:
        """True for statuses that count toward progress (done + closed)."""
        return self.category in (StatusCategory.done, StatusCategory.closed)

    def __repr__(self) -> str:
        return f"<IssueStatus id={self.id} name={self.name!r} category={self.category!r}>"


class Tracker(Base):
    """Issue tracker type (Bug, Feature, Task, Support, …).

    Each tracker can have a default status applied when issues of that type
    are first created.  ``disabled_core_fields`` replaces the legacy bitmask
    with an explicit JSONB list of field names to hide in the UI.
    """

    __tablename__ = "trackers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    name: Mapped[str] = mapped_column(String(255), nullable=False)

    # FK to issue_statuses; RESTRICT so the status cannot be deleted while in use
    default_status_id: Mapped[int | None] = mapped_column(
        ForeignKey("issue_statuses.id", ondelete="RESTRICT"),
        nullable=True,
    )

    is_in_roadmap: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")

    position: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # List of field names hidden from the UI for this tracker type.
    # Replaces the legacy numeric bitmask with a human-readable JSONB list.
    # Example: ["due_date", "estimated_hours"]
    disabled_core_fields: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")

    def __repr__(self) -> str:
        return f"<Tracker id={self.id} name={self.name!r}>"


class IssuePriority(Base):
    """Priority level for issues (Low, Normal, High, Urgent, Immediate).

    ``is_default`` marks the priority applied when none is explicitly set.
    Only one priority should have ``is_default=True`` at a time — enforced
    at the service layer.
    """

    __tablename__ = "issue_priorities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    name: Mapped[str] = mapped_column(String(255), nullable=False)

    position: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")

    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")

    def __repr__(self) -> str:
        return f"<IssuePriority id={self.id} name={self.name!r} is_default={self.is_default}>"


class IssueCategory(Base):
    """Project-scoped issue category (e.g. "Backend", "Frontend", "CI/CD").

    Each category belongs to exactly one project.  The optional
    ``assigned_to_id`` sets the default assignee when issues are created
    in this category.
    """

    __tablename__ = "issue_categories"

    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_issue_categories_project_name"),
        Index("ix_issue_categories_project_id", "project_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)

    # Default assignee for issues in this category; cleared if user is deleted
    assigned_to_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    def __repr__(self) -> str:
        return f"<IssueCategory id={self.id} project_id={self.project_id} name={self.name!r}>"
