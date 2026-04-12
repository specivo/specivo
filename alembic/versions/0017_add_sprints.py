"""Add sprints table and sprint_id to issues.

Create Date: 2026-04-12
Revision ID: 0017
Revises: 0016

Adds the sprints table for time-boxed iterations and a sprint_id
foreign key on the issues table.
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0017"
down_revision = "0016"


def upgrade() -> None:
    op.create_table(
        "sprints",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Integer(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("goal", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.String(30),
            nullable=False,
            server_default="planned",
        ),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("velocity_snapshot", JSONB(), nullable=True),
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
            "status IN ('planned','active','completed')",
            name="ck_sprints_status",
        ),
    )

    op.create_index("ix_sprints_project_id", "sprints", ["project_id"])
    op.create_index(
        "uq_sprints_project_active",
        "sprints",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    # Add sprint_id to issues
    op.add_column(
        "issues",
        sa.Column(
            "sprint_id",
            sa.Integer(),
            sa.ForeignKey("sprints.id", ondelete="SET NULL", name="fk_issues_sprint_id"),
            nullable=True,
        ),
    )
    op.create_index("ix_issues_sprint_id", "issues", ["sprint_id"])


def downgrade() -> None:
    op.drop_index("ix_issues_sprint_id", table_name="issues")
    op.drop_constraint("fk_issues_sprint_id", "issues", type_="foreignkey")
    op.drop_column("issues", "sprint_id")
    op.drop_index("uq_sprints_project_active", table_name="sprints")
    op.drop_index("ix_sprints_project_id", table_name="sprints")
    op.drop_table("sprints")
