"""AI usage entries follow issues that moved to another project.

Create Date: 2026-11-12
Revision ID: 0069
Revises: 0068

Moving an issue to another project re-synced ``project_id`` on its journals,
time entries and running timers, but not on its AI usage entries. Such entries
kept the old project, so they dropped out of the new project's reports and,
in the old project, out of sight of everyone but their owner.

- Backfill: every AI usage entry whose issue now lives in another project gets
  that issue's project. The move itself now does the same.

The downgrade leaves the data as it is: the old attribution was wrong, and the
previous project of a moved entry is not recorded anywhere.
"""

from alembic import op

revision = "0069"
down_revision = "0068"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE ai_usage_entries AS e
        SET project_id = i.project_id
        FROM issues AS i
        WHERE i.id = e.issue_id
          AND e.project_id IS DISTINCT FROM i.project_id
        """
    )


def downgrade() -> None:
    pass
