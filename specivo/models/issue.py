"""Issue model — the core entity of the Specivo tracker."""

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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from specivo.models.base import Base, LockVersionMixin, TimestampMixin


class Issue(Base, TimestampMixin, LockVersionMixin):
    """A tracker issue belonging to a project.

    Identity
    --------
    Every issue has two identifiers:
    - ``id``: internal DB primary key (used for FK references, never shown to users)
    - ``display_key``: computed ``"{project_key}-{sequence_number}"`` (e.g. ACME-42)

    The ``project_key`` is denormalised from ``projects.key`` so that display
    keys can be constructed without a JOIN on every list or search result.

    Sequence
    --------
    ``sequence_number`` is assigned atomically via::

        UPDATE projects
        SET issue_sequence = issue_sequence + 1
        WHERE id = :project_id
        RETURNING issue_sequence, key

    This guarantees per-project sequential numbers with no gaps under
    concurrent inserts.

    Hierarchy (Nested Set — Phase 1.5)
    -----------------------------------
    ``parent_id``, ``root_id``, ``lft``, ``rgt`` are present but not enforced
    in Phase 1 MVP.  The NestedSetService will populate them properly.
    ``lft``/``rgt`` default to 1/2 (a leaf node) so the column is NOT NULL.

    Estimates
    ---------
    All hour fields use ``Numeric(10, 2)`` — *not* Float — to avoid IEEE 754
    rounding errors in financial / time calculations (I6 fix).
    """

    __tablename__ = "issues"

    __table_args__ = (
        # Display key must be unique: no two issues in the same project share a number
        UniqueConstraint("project_key", "sequence_number", name="uq_issue_display_key"),
        # Composite index for fast "ACME-42" lookups (used in every GET /issues/{ref})
        Index("idx_issue_display_key", "project_key", "sequence_number"),
        # Additional FK indexes (I2 fix: PostgreSQL does NOT auto-index FK columns)
        Index("ix_issues_project_id", "project_id"),
        Index("ix_issues_status_id", "status_id"),
        Index("ix_issues_assigned_to_id", "assigned_to_id"),
        Index("ix_issues_parent_id", "parent_id"),
        Index("ix_issues_fixed_version_id", "fixed_version_id"),
        # FK indexes — PostgreSQL does NOT auto-index FK columns
        Index("ix_issues_tracker_id", "tracker_id"),
        Index("ix_issues_priority_id", "priority_id"),
        Index("ix_issues_author_id", "author_id"),
        Index("ix_issues_category_id", "category_id"),
        Index("ix_issues_sprint_id", "sprint_id"),
        Index("ix_issues_updated_at", "updated_at"),
        # GIN index on metadata JSONB for key/value queries
        Index("ix_issues_metadata_gin", "issue_metadata", postgresql_using="gin"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Denormalised from projects.key — immutable once set.
    # Allows display key construction without a JOIN.
    project_key: Mapped[str] = mapped_column(String(128), nullable=False)

    # Per-project sequential number; assigned atomically on create
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)

    # ------------------------------------------------------------------
    # Classification
    # ------------------------------------------------------------------

    tracker_id: Mapped[int] = mapped_column(
        ForeignKey("trackers.id", ondelete="RESTRICT"),
        nullable=False,
    )

    status_id: Mapped[int] = mapped_column(
        ForeignKey("issue_statuses.id", ondelete="RESTRICT"),
        nullable=False,
    )

    priority_id: Mapped[int] = mapped_column(
        ForeignKey("issue_priorities.id", ondelete="RESTRICT"),
        nullable=False,
    )

    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("issue_categories.id", ondelete="SET NULL"),
        nullable=True,
    )

    # ------------------------------------------------------------------
    # People
    # ------------------------------------------------------------------

    author_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    assigned_to_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    # ------------------------------------------------------------------
    # Content
    # ------------------------------------------------------------------

    subject: Mapped[str] = mapped_column(String(1024), nullable=False)

    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Extensible metadata bag — GIN indexed for key/value queries.
    # Metadata stored as JSONB; schema validation via MetadataSchemaService
    issue_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")

    # ------------------------------------------------------------------
    # Nested Set (nullable until subtask hierarchy is populated)
    # ------------------------------------------------------------------

    # Self-referential parent link; cleared when parent is deleted
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("issues.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Root of the nested-set tree; cleared when root is deleted (cascade)
    root_id: Mapped[int | None] = mapped_column(
        ForeignKey("issues.id", ondelete="CASCADE"),
        nullable=True,
    )

    # Nested set boundary values (leaf node defaults: lft=1, rgt=2)
    lft: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    rgt: Mapped[int] = mapped_column(Integer, nullable=False, default=2, server_default="2")

    # ------------------------------------------------------------------
    # Planning
    # ------------------------------------------------------------------

    # FK to versions table (added in M1.6); SET NULL if version is deleted.
    # use_alter=True defers constraint creation so the issues table can be
    # created before the versions table exists.
    fixed_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("versions.id", ondelete="SET NULL", use_alter=True, name="fk_issues_fixed_version_id"),
        nullable=True,
    )

    # FK to sprints table; SET NULL if sprint is deleted.
    sprint_id: Mapped[int | None] = mapped_column(
        ForeignKey("sprints.id", ondelete="SET NULL", use_alter=True, name="fk_issues_sprint_id"),
        nullable=True,
    )

    done_ratio: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    # Numeric(10, 2) — NOT Float — to avoid IEEE 754 rounding (I6 fix)
    estimated_hours: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    original_estimate: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    remaining_estimate: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)

    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    closed_on: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    is_private: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")

    # ------------------------------------------------------------------
    # Relationships (lazy="raise" — always use selectinload explicitly)
    # ------------------------------------------------------------------

    tracker = relationship("Tracker", foreign_keys=[tracker_id], lazy="raise")
    status = relationship("IssueStatus", foreign_keys=[status_id], lazy="raise")
    priority = relationship("IssuePriority", foreign_keys=[priority_id], lazy="raise")
    category = relationship("IssueCategory", foreign_keys=[category_id], lazy="raise")
    author = relationship("User", foreign_keys=[author_id], lazy="raise")
    assigned_to = relationship("User", foreign_keys=[assigned_to_id], lazy="raise")
    fixed_version = relationship("Version", foreign_keys=[fixed_version_id], lazy="raise")
    sprint = relationship("Sprint", foreign_keys=[sprint_id], lazy="raise")
    project = relationship("Project", foreign_keys=[project_id], lazy="raise")

    # ------------------------------------------------------------------
    # Computed property
    # ------------------------------------------------------------------

    @property
    def display_key(self) -> str:
        """Human-readable issue key shown in the UI (e.g. 'ACME-42')."""
        return f"{self.project_key}-{self.sequence_number}"

    def __repr__(self) -> str:
        return f"<Issue id={self.id} key={self.display_key!r} subject={self.subject!r}>"
