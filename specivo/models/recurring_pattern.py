"""RecurringPattern model — a project-owned template that spawns issues on a schedule.

A pattern is *not* an issue. It holds the template field values plus an RFC 5545
RRULE-shaped recurrence rule, extended with tracker-style concepts that pure RRULE
cannot express:

- ``anchor_mode`` — ``fixed`` generates every occurrence on schedule regardless of
  whether the previous instance is closed (overdue instances stack); ``flexible``
  generates the next occurrence only after the previous one closes.
- ``base_date_strategy`` — for flexible mode, whether the next occurrence is anchored
  to the previous *scheduled* date or its *completion* date.
- working-day handling — shift occurrences off non-working days (no RRULE token exists).
- ``creation_lead_time_days`` — bounded look-ahead window; the generator materialises
  occurrences up to this many days ahead, never the full (possibly infinite) series.

Each generated :class:`~specivo.models.issue.Issue` carries ``recurring_pattern_id`` and
``original_occurrence_at`` (the scheduled occurrence datetime) so generation is idempotent
and overrides/skips can be tracked via :class:`~specivo.models.recurrence_exception.RecurrenceException`.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from specivo.models.base import Base, LockVersionMixin, TimestampMixin


class RecurringPattern(Base, TimestampMixin, LockVersionMixin):
    """A project-owned recurrence rule plus the issue template it generates."""

    __tablename__ = "recurring_patterns"

    __table_args__ = (
        Index("ix_recurring_patterns_project_id", "project_id"),
        # Partial index so the generator can cheaply scan only live patterns.
        Index(
            "ix_recurring_patterns_enabled",
            "enabled",
            postgresql_where=text("enabled = true"),
        ),
        # GIN index on the template metadata bag for key/value queries.
        Index("ix_recurring_patterns_metadata_gin", "template_metadata", postgresql_using="gin"),
        CheckConstraint(
            "freq IN ('daily','weekly','monthly','yearly')",
            name="ck_recurring_patterns_freq",
        ),
        CheckConstraint("rrule_interval > 0", name="ck_recurring_patterns_interval"),
        CheckConstraint(
            "anchor_mode IN ('fixed','flexible')",
            name="ck_recurring_patterns_anchor_mode",
        ),
        CheckConstraint(
            "base_date_strategy IN ('scheduled','completion')",
            name="ck_recurring_patterns_base_date_strategy",
        ),
        CheckConstraint(
            "working_day_adjustment IN ('none','nearest','next','previous')",
            name="ck_recurring_patterns_working_day_adjustment",
        ),
        # COUNT and UNTIL are mutually exclusive end conditions (RFC 5545).
        CheckConstraint(
            "NOT (rrule_count IS NOT NULL AND until IS NOT NULL)",
            name="ck_recurring_patterns_count_xor_until",
        ),
        CheckConstraint(
            "creation_lead_time_days > 0",
            name="ck_recurring_patterns_lead_time",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # ------------------------------------------------------------------
    # Ownership — project-owned so the series survives member departure.
    # ------------------------------------------------------------------

    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Creator; attribution for generated issues. RESTRICT mirrors Issue.author_id —
    # deactivated users still exist in the DB, so generation keeps running.
    author_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)

    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")

    # ------------------------------------------------------------------
    # Issue template — the field values each generated instance is born with.
    # ------------------------------------------------------------------

    template_tracker_id: Mapped[int] = mapped_column(
        ForeignKey("trackers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    template_status_id: Mapped[int | None] = mapped_column(
        ForeignKey("issue_statuses.id", ondelete="RESTRICT"),
        nullable=True,
    )
    template_priority_id: Mapped[int | None] = mapped_column(
        ForeignKey("issue_priorities.id", ondelete="RESTRICT"),
        nullable=True,
    )
    template_category_id: Mapped[int | None] = mapped_column(
        ForeignKey("issue_categories.id", ondelete="SET NULL"),
        nullable=True,
    )
    template_assigned_to_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    template_fixed_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("versions.id", ondelete="SET NULL", use_alter=True, name="fk_recurring_patterns_version_id"),
        nullable=True,
    )
    # Note: by default the generator does NOT pin instances to a fixed sprint — see
    # RecurringPatternService — to avoid skewing velocity. Stored for opt-in use.
    template_sprint_id: Mapped[int | None] = mapped_column(
        ForeignKey("sprints.id", ondelete="SET NULL", use_alter=True, name="fk_recurring_patterns_sprint_id"),
        nullable=True,
    )

    template_subject: Mapped[str] = mapped_column(String(1024), nullable=False)
    template_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    template_estimated_hours: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    template_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    is_private: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")

    # ------------------------------------------------------------------
    # Recurrence rule (RFC 5545 subset). ``rrule_raw`` is an escape hatch that,
    # when set, takes precedence over the discrete fields in the expansion engine.
    # ------------------------------------------------------------------

    freq: Mapped[str] = mapped_column(String(10), nullable=False)
    rrule_interval: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    # JSONB arrays mirroring RRULE BY* parts. byday entries may carry an ordinal
    # prefix for nth-weekday semantics, e.g. ["MO","WE"] or ["1MO","-1FR"].
    byday: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    bymonthday: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    bymonth: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    bysetpos: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    rrule_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rrule_raw: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ------------------------------------------------------------------
    # Tracker-style extensions (the part pure RRULE cannot express).
    # ------------------------------------------------------------------

    anchor_mode: Mapped[str] = mapped_column(
        String(10), nullable=False, default="fixed", server_default="fixed"
    )
    base_date_strategy: Mapped[str] = mapped_column(
        String(12), nullable=False, default="scheduled", server_default="scheduled"
    )
    # Series anchor, interpreted as local wall-clock in ``timezone``.
    dtstart: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC", server_default="UTC")

    working_day_adjustment: Mapped[str] = mapped_column(
        String(10), nullable=False, default="none", server_default="none"
    )
    # ISO weekday ints (1=Mon … 7=Sun) considered working days.
    working_days: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=lambda: [1, 2, 3, 4, 5], server_default="[1, 2, 3, 4, 5]"
    )
    # Explicit list of ISO date strings treated as non-working (holidays).
    holiday_calendar: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    creation_lead_time_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30, server_default="30"
    )

    # ------------------------------------------------------------------
    # Carry-over / reset / rotation config.
    # ------------------------------------------------------------------

    # Per-field-group booleans controlling what copies from the previous instance.
    # Relations/attachments default OFF (see RecurringPatternService risk notes).
    carry_over: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    reset_checklist: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    # e.g. {"user_ids": [3, 7, 9], "strategy": "round_robin"}
    assignee_rotation: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    rotation_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    # Offsets deriving start/due dates from each occurrence datetime.
    start_offset_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    due_offset_days: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # ------------------------------------------------------------------
    # Bookkeeping (observability only — never used for dedupe).
    # ------------------------------------------------------------------

    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_generated_occurrence_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ------------------------------------------------------------------
    # Relationships (lazy="raise" — always use selectinload explicitly).
    # ------------------------------------------------------------------

    project = relationship("Project", foreign_keys=[project_id], lazy="raise")
    author = relationship("User", foreign_keys=[author_id], lazy="raise")
    template_tracker = relationship("Tracker", foreign_keys=[template_tracker_id], lazy="raise")
    exceptions = relationship(
        "RecurrenceException",
        back_populates="pattern",
        cascade="all, delete-orphan",
        lazy="raise",
    )

    def __repr__(self) -> str:
        return (
            f"<RecurringPattern id={self.id} name={self.name!r} "
            f"project_id={self.project_id} freq={self.freq!r} anchor={self.anchor_mode!r}>"
        )
