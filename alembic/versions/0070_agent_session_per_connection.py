"""Agent sessions belong to one MCP connection.

Create Date: 2026-11-12
Revision ID: 0070
Revises: 0069

An agent session used to be the latest activity window of an API key, so two
MCP connections made with one key (two terminal sessions of one person, or a
new session started soon after the last one) shared a session and the issue
it was working on.

- ``agent_sessions.connection_hash`` (String(64), nullable) is added: the
  SHA-256 hex digest of the MCP transport session id (``Mcp-Session-Id`` for
  Streamable HTTP, the session id of a legacy SSE stream). ``NULL`` for
  sessions of plain REST calls and for existing rows.
- ``(api_key_id, connection_hash, last_activity_at)`` is indexed for the
  per-request "latest session of this connection" lookup.

The downgrade drops the index and the column.
"""

import sqlalchemy as sa

from alembic import op

revision = "0070"
down_revision = "0069"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_sessions", sa.Column("connection_hash", sa.String(length=64), nullable=True))
    op.create_index(
        "ix_agent_sessions_api_key_id_connection_hash_last_activity_at",
        "agent_sessions",
        ["api_key_id", "connection_hash", "last_activity_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_agent_sessions_api_key_id_connection_hash_last_activity_at", table_name="agent_sessions")
    op.drop_column("agent_sessions", "connection_hash")
