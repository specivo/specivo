"""Agent sessions record the client that opened them, and index their time windows.

Create Date: 2026-11-06
Revision ID: 0064
Revises: 0063

- ``agent_sessions.model_name`` is renamed to ``client_name``: it holds the
  MCP ``clientInfo`` name (or the User-Agent product), never a model.
- ``agent_sessions.client_version`` (String(50), nullable) is added, matching
  ``ai_usage_entries.client_name`` / ``client_version``.
- The single-column indexes on ``api_key_id`` and ``user_id`` are replaced by
  ``(api_key_id, last_activity_at)``, which serves the per-request "latest
  session of this key" lookup, and ``(user_id, last_activity_at)``, which
  serves ``AgentSessionService.issue_for`` (the session of a user overlapping
  a time window).

The downgrade restores the column name, drops ``client_version`` and puts the
single-column indexes back.
"""

import sqlalchemy as sa

from alembic import op

revision = "0064"
down_revision = "0063"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("agent_sessions", "model_name", new_column_name="client_name")
    op.add_column("agent_sessions", sa.Column("client_version", sa.String(length=50), nullable=True))

    op.drop_index("ix_agent_sessions_api_key_id", table_name="agent_sessions")
    op.drop_index("ix_agent_sessions_user_id", table_name="agent_sessions")
    op.create_index(
        "ix_agent_sessions_api_key_id_last_activity_at",
        "agent_sessions",
        ["api_key_id", "last_activity_at"],
        unique=False,
    )
    op.create_index(
        "ix_agent_sessions_user_id_last_activity_at",
        "agent_sessions",
        ["user_id", "last_activity_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_agent_sessions_user_id_last_activity_at", table_name="agent_sessions")
    op.drop_index("ix_agent_sessions_api_key_id_last_activity_at", table_name="agent_sessions")
    op.create_index("ix_agent_sessions_user_id", "agent_sessions", ["user_id"], unique=False)
    op.create_index("ix_agent_sessions_api_key_id", "agent_sessions", ["api_key_id"], unique=False)

    op.drop_column("agent_sessions", "client_version")
    op.alter_column("agent_sessions", "client_name", new_column_name="model_name")
