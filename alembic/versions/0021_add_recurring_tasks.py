"""Add recurring tasks: recurring_patterns, recurrence_exceptions, issue columns.

Create Date: 2026-06-17
Revision ID: 0021
Revises: 0020

Adds the recurring_patterns table (a project-owned recurrence rule plus the
issue template it generates), the recurrence_exceptions table (per-occurrence
skip / override), and two columns on issues (recurring_pattern_id,
original_occurrence_at) plus a partial unique index that acts as the DB-level
idempotency guard for generated issues.
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # recurring_patterns
    # ------------------------------------------------------------------
    op.create_table(
        "recurring_patterns",
        sa.Column("id", sa.Integer(), primary_key=True),
        # Ownership
        sa.Column(
            "project_id",
            sa.Integer(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "author_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        # Issue template
        sa.Column(
            "template_tracker_id",
            sa.Integer(),
            sa.ForeignKey("trackers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "template_status_id",
            sa.Integer(),
            sa.ForeignKey("issue_statuses.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "template_priority_id",
            sa.Integer(),
            sa.ForeignKey("issue_priorities.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "template_category_id",
            sa.Integer(),
            sa.ForeignKey("issue_categories.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "template_assigned_to_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "template_fixed_version_id",
            sa.Integer(),
            sa.ForeignKey(
                "versions.id",
                ondelete="SET NULL",
                name="fk_recurring_patterns_version_id",
            ),
            nullable=True,
        ),
        sa.Column(
            "template_sprint_id",
            sa.Integer(),
            sa.ForeignKey(
                "sprints.id",
                ondelete="SET NULL",
                name="fk_recurring_patterns_sprint_id",
            ),
            nullable=True,
        ),
        sa.Column("template_subject", sa.String(1024), nullable=False),
        sa.Column("template_description", sa.Text(), nullable=True),
        sa.Column("template_estimated_hours", sa.Numeric(10, 2), nullable=True),
        sa.Column("template_metadata", JSONB(), nullable=False, server_default="{}"),
        sa.Column("is_private", sa.Boolean(), nullable=False, server_default="false"),
        # Recurrence rule (RFC 5545 subset)
        sa.Column("freq", sa.String(10), nullable=False),
        sa.Column("rrule_interval", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("byday", JSONB(), nullable=True),
        sa.Column("bymonthday", JSONB(), nullable=True),
        sa.Column("bymonth", JSONB(), nullable=True),
        sa.Column("bysetpos", JSONB(), nullable=True),
        sa.Column("rrule_count", sa.Integer(), nullable=True),
        sa.Column("until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rrule_raw", sa.Text(), nullable=True),
        # Tracker-style extensions
        sa.Column("anchor_mode", sa.String(10), nullable=False, server_default="fixed"),
        sa.Column(
            "base_date_strategy",
            sa.String(12),
            nullable=False,
            server_default="scheduled",
        ),
        sa.Column("dtstart", sa.DateTime(timezone=True), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="UTC"),
        sa.Column(
            "working_day_adjustment",
            sa.String(10),
            nullable=False,
            server_default="none",
        ),
        sa.Column(
            "working_days",
            JSONB(),
            nullable=False,
            server_default="[1, 2, 3, 4, 5]",
        ),
        sa.Column("holiday_calendar", JSONB(), nullable=True),
        sa.Column(
            "creation_lead_time_days",
            sa.Integer(),
            nullable=False,
            server_default="30",
        ),
        # Carry-over / reset / rotation config
        sa.Column("carry_over", JSONB(), nullable=False, server_default="{}"),
        sa.Column("reset_checklist", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("assignee_rotation", JSONB(), nullable=True),
        sa.Column("rotation_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("start_offset_days", sa.Integer(), nullable=True),
        sa.Column("due_offset_days", sa.Integer(), nullable=True),
        # Bookkeeping
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "last_generated_occurrence_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        # Optimistic locking + timestamps
        sa.Column("lock_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "freq IN ('daily','weekly','monthly','yearly')",
            name="ck_recurring_patterns_freq",
        ),
        sa.CheckConstraint("rrule_interval > 0", name="ck_recurring_patterns_interval"),
        sa.CheckConstraint(
            "anchor_mode IN ('fixed','flexible')",
            name="ck_recurring_patterns_anchor_mode",
        ),
        sa.CheckConstraint(
            "base_date_strategy IN ('scheduled','completion')",
            name="ck_recurring_patterns_base_date_strategy",
        ),
        sa.CheckConstraint(
            "working_day_adjustment IN ('none','nearest','next','previous')",
            name="ck_recurring_patterns_working_day_adjustment",
        ),
        sa.CheckConstraint(
            "NOT (rrule_count IS NOT NULL AND until IS NOT NULL)",
            name="ck_recurring_patterns_count_xor_until",
        ),
        sa.CheckConstraint(
            "creation_lead_time_days > 0",
            name="ck_recurring_patterns_lead_time",
        ),
    )

    op.create_index(
        "ix_recurring_patterns_project_id",
        "recurring_patterns",
        ["project_id"],
    )
    op.create_index(
        "ix_recurring_patterns_enabled",
        "recurring_patterns",
        ["enabled"],
        postgresql_where=sa.text("enabled = true"),
    )
    op.create_index(
        "ix_recurring_patterns_metadata_gin",
        "recurring_patterns",
        ["template_metadata"],
        postgresql_using="gin",
    )

    # ------------------------------------------------------------------
    # recurrence_exceptions
    # ------------------------------------------------------------------
    op.create_table(
        "recurrence_exceptions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "recurring_pattern_id",
            sa.Integer(),
            sa.ForeignKey("recurring_patterns.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("occurrence_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("override_payload", JSONB(), nullable=True),
        sa.Column(
            "materialized_issue_id",
            sa.Integer(),
            sa.ForeignKey("issues.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "recurring_pattern_id",
            "occurrence_at",
            name="uq_recurrence_exception",
        ),
        sa.CheckConstraint(
            "kind IN ('skip','override')",
            name="ck_recurrence_exceptions_kind",
        ),
    )

    op.create_index(
        "ix_recurrence_exceptions_pattern_id",
        "recurrence_exceptions",
        ["recurring_pattern_id"],
    )

    # ------------------------------------------------------------------
    # issues: recurrence columns
    # ------------------------------------------------------------------
    op.add_column(
        "issues",
        sa.Column(
            "recurring_pattern_id",
            sa.Integer(),
            sa.ForeignKey(
                "recurring_patterns.id",
                ondelete="SET NULL",
                name="fk_issues_recurring_pattern_id",
            ),
            nullable=True,
        ),
    )
    op.add_column(
        "issues",
        sa.Column("original_occurrence_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_issues_recurring_pattern_id",
        "issues",
        ["recurring_pattern_id"],
    )
    op.create_index(
        "uq_issue_occurrence",
        "issues",
        ["recurring_pattern_id", "original_occurrence_at"],
        unique=True,
        postgresql_where=sa.text("recurring_pattern_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_issue_occurrence", table_name="issues")
    op.drop_index("ix_issues_recurring_pattern_id", table_name="issues")
    op.drop_constraint("fk_issues_recurring_pattern_id", "issues", type_="foreignkey")
    op.drop_column("issues", "original_occurrence_at")
    op.drop_column("issues", "recurring_pattern_id")

    op.drop_index(
        "ix_recurrence_exceptions_pattern_id",
        table_name="recurrence_exceptions",
    )
    op.drop_table("recurrence_exceptions")

    op.drop_index(
        "ix_recurring_patterns_metadata_gin",
        table_name="recurring_patterns",
    )
    op.drop_index("ix_recurring_patterns_enabled", table_name="recurring_patterns")
    op.drop_index("ix_recurring_patterns_project_id", table_name="recurring_patterns")
    op.drop_table("recurring_patterns")
