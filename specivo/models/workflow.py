"""Workflow models: transition rules and field behavior per tracker/role/status."""

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from specivo.models.base import Base


class WorkflowTransition(Base):
    """Defines allowed status transitions per tracker and role."""

    __tablename__ = "workflow_transitions"

    __table_args__ = (
        UniqueConstraint(
            "tracker_id",
            "role_id",
            "old_status_id",
            "new_status_id",
            name="uq_workflow_transition",
        ),
        Index(
            "ix_wf_transitions_lookup",
            "tracker_id",
            "role_id",
            "old_status_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    tracker_id: Mapped[int] = mapped_column(
        ForeignKey("trackers.id", ondelete="CASCADE"),
        nullable=False,
    )

    role_id: Mapped[int] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"),
        nullable=False,
    )

    old_status_id: Mapped[int] = mapped_column(
        ForeignKey("issue_statuses.id", ondelete="CASCADE"),
        nullable=False,
    )

    new_status_id: Mapped[int] = mapped_column(
        ForeignKey("issue_statuses.id", ondelete="CASCADE"),
        nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<WorkflowTransition id={self.id} "
            f"tracker={self.tracker_id} role={self.role_id} "
            f"{self.old_status_id}->{self.new_status_id}>"
        )


class WorkflowFieldRule(Base):
    """Defines field behavior (required/readonly) per tracker, role, and status."""

    __tablename__ = "workflow_field_rules"

    __table_args__ = (
        UniqueConstraint(
            "tracker_id",
            "role_id",
            "status_id",
            "field_name",
            name="uq_workflow_field_rule",
        ),
        CheckConstraint(
            "rule IN ('required', 'readonly')",
            name="ck_workflow_field_rule_type",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    tracker_id: Mapped[int] = mapped_column(
        ForeignKey("trackers.id", ondelete="CASCADE"),
        nullable=False,
    )

    role_id: Mapped[int] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"),
        nullable=False,
    )

    status_id: Mapped[int] = mapped_column(
        ForeignKey("issue_statuses.id", ondelete="CASCADE"),
        nullable=False,
    )

    field_name: Mapped[str] = mapped_column(String(64), nullable=False)

    rule: Mapped[str] = mapped_column(String(20), nullable=False)

    def __repr__(self) -> str:
        return (
            f"<WorkflowFieldRule id={self.id} "
            f"tracker={self.tracker_id} role={self.role_id} "
            f"status={self.status_id} {self.field_name}={self.rule}>"
        )
