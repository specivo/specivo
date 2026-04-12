"""Replace is_closed boolean with category enum on issue_statuses.

Categories: backlog, active, done, closed.

Revision ID: 0011
Revises: 0010
Create Date: 2026-04-05
"""

import sqlalchemy as sa

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add category column with a temporary default
    op.add_column(
        "issue_statuses",
        sa.Column("category", sa.String(20), nullable=False, server_default="backlog"),
    )

    # 2. Populate category from existing data
    op.execute(
        """
        UPDATE issue_statuses SET category = CASE
            WHEN name = 'In Progress' THEN 'active'
            WHEN name = 'Feedback'    THEN 'active'
            WHEN name = 'Resolved'    THEN 'done'
            WHEN is_closed = true     THEN 'closed'
            ELSE 'backlog'
        END
        """
    )

    # 3. Drop old is_closed column
    op.drop_column("issue_statuses", "is_closed")

    # 4. Add CHECK constraint
    op.create_check_constraint(
        "ck_issue_statuses_category",
        "issue_statuses",
        "category IN ('backlog', 'active', 'done', 'closed')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_issue_statuses_category", "issue_statuses", type_="check")

    op.add_column(
        "issue_statuses",
        sa.Column("is_closed", sa.Boolean(), nullable=False, server_default="false"),
    )

    op.execute(
        """
        UPDATE issue_statuses SET is_closed = CASE
            WHEN category = 'closed' THEN true
            ELSE false
        END
        """
    )

    op.drop_column("issue_statuses", "category")
