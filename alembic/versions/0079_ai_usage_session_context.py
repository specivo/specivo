"""AI usage entries name their parent session, whether a person attended the run, and the agent profile.

Create Date: 2026-11-23
Revision ID: 0079
Revises: 0078

Contract v1 gains three optional record properties for agents that run
subagents, scheduled jobs and several profiles. Entries store them:

- ``parent_session_id`` (String(200), nullable): the client's parent session
  (a subagent's, a branch's, a continuation after context compression),
  opaque like ``external_session_id``.
- ``unattended`` (Boolean, NOT NULL, default false): a run with nobody in the
  loop (cron, scheduled). Existing entries read as attended.
- ``profile`` (String(100), nullable): the agent profile or home the usage
  came from, stored verbatim.

Index ``ix_ai_usage_entries_project_unattended_date (project_id, unattended,
usage_date)`` serves the spend report's attended/unattended split.

The downgrade drops the index and the three columns.
"""

import sqlalchemy as sa

from alembic import op

revision = "0079"
down_revision = "0078"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ai_usage_entries", sa.Column("parent_session_id", sa.String(length=200), nullable=True))
    op.add_column(
        "ai_usage_entries",
        sa.Column("unattended", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("ai_usage_entries", sa.Column("profile", sa.String(length=100), nullable=True))
    op.create_index(
        "ix_ai_usage_entries_project_unattended_date",
        "ai_usage_entries",
        ["project_id", "unattended", "usage_date"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_ai_usage_entries_project_unattended_date", table_name="ai_usage_entries")
    op.drop_column("ai_usage_entries", "profile")
    op.drop_column("ai_usage_entries", "unattended")
    op.drop_column("ai_usage_entries", "parent_session_id")
