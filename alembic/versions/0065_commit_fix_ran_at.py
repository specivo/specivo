"""Commit links record when their ``Fixes`` command ran.

Create Date: 2026-11-06
Revision ID: 0065
Revises: 0064

- ``issue_commit_links.fix_ran_at`` (timestamptz, nullable) is added. A
  fixing commit first pushed to another branch now runs its command when it
  reaches the default branch; the column makes that happen once per
  (commit, issue), whatever the outcome.
- Backfill: fixing links whose commit was first seen on the integration's
  default branch already ran their command, so they get ``created_at``.
  Fixing links from other branches stay NULL and run when their commit is
  next pushed to the default branch.

The downgrade drops the column.
"""

import sqlalchemy as sa

from alembic import op

revision = "0065"
down_revision = "0064"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("issue_commit_links", sa.Column("fix_ran_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(
        """
        UPDATE issue_commit_links AS l
        SET fix_ran_at = l.created_at
        FROM repository_commits AS c
        JOIN repository_integrations AS r ON r.id = c.integration_id
        WHERE c.id = l.commit_id
          AND l.source = 'message'
          AND l.keyword = 'fixes'
          AND c.branch = r.default_branch
        """
    )


def downgrade() -> None:
    op.drop_column("issue_commit_links", "fix_ran_at")
