"""Audit rows name the agent session of an MCP tool call; sessions count their tool calls.

Create Date: 2026-11-23
Revision ID: 0078
Revises: 0077

The security audit log recorded every MCP tool call without the agent session
it belonged to, so a session's calls could not be listed.

- ``security_audit_logs.agent_session_id`` (Integer, nullable): the agent
  session of the tool call. No foreign key, as for ``user_id`` and
  ``project_id``: audit rows outlive the sessions and the table is
  partitioned. Index ``ix_security_audit_agent_session_created
  (agent_session_id, created_at)``, created on the partitioned parent so every
  monthly partition gets it.
- ``agent_sessions.tool_call_count`` (Integer, not null, default 0): the
  number of tool calls written for the session, kept so the session list needs
  no join to the partitioned audit table. Existing sessions start at 0: their
  older audit rows carry no session id.
"""

import sqlalchemy as sa

from alembic import op

revision = "0078"
down_revision = "0077"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("security_audit_logs", sa.Column("agent_session_id", sa.Integer(), nullable=True))
    op.create_index(
        "ix_security_audit_agent_session_created",
        "security_audit_logs",
        ["agent_session_id", "created_at"],
        unique=False,
    )
    op.add_column(
        "agent_sessions",
        sa.Column("tool_call_count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("agent_sessions", "tool_call_count")
    op.drop_index("ix_security_audit_agent_session_created", table_name="security_audit_logs")
    op.drop_column("security_audit_logs", "agent_session_id")
