"""Index issues on (root_id, lft) for the nested-set shifts.

Create Date: 2026-11-11
Revision ID: 0068
Revises: 0067

Every subtask insert or move shifts the ``lft``/``rgt`` values of the issues in
one tree (``WHERE root_id = :r AND lft >= :x``) while holding the tree's
advisory lock. Without an index on ``root_id`` that is a scan of the whole
``issues`` table each time.
"""

from alembic import op

revision = "0068"
down_revision = "0067"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_issues_root_id_lft", "issues", ["root_id", "lft"])


def downgrade() -> None:
    op.drop_index("ix_issues_root_id_lft", table_name="issues")
