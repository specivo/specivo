"""Agent sessions record the client's own session id, its parent, how they were opened and when they ended.

Create Date: 2026-11-23
Revision ID: 0075
Revises: 0073

An agent session was linked to AI usage only by time overlap. The client's own
session id (for example a Hermes ``session_id``) and its parent let the
reporter, the event receiver and the issue timeline link by id instead.

- ``external_session_id`` (String(200), nullable): the client's session id,
  opaque text. Not unique: a reconnect opens another connection row with the
  same id, and the newest one wins.
- ``parent_external_session_id`` (String(200), nullable): the client's parent
  session id, kept raw so a parent reported later can still be linked.
- ``parent_session_id`` (Integer, nullable, FK ``agent_sessions.id`` ON DELETE
  SET NULL): the resolved parent row, always of the same user.
- ``client_type`` (String(16), nullable, ``mcp`` / ``rest`` / ``webhook``): how
  the row was opened. Existing rows: ``mcp`` when they have a
  ``connection_hash``, else ``rest``.
- ``ended_at`` (timestamptz, nullable): set when a client reports the end of a
  session; ``NULL`` for sessions that only time out.
- ``api_key_id`` becomes nullable, for sessions an event receiver opens without
  a key: ``CHECK (api_key_id IS NOT NULL OR client_type = 'webhook')``.
- Indexes ``(user_id, external_session_id)`` and ``(parent_session_id)``.

The downgrade deletes the sessions without an API key, makes ``api_key_id``
required again and drops the indexes, constraints and columns.
"""

import sqlalchemy as sa

from alembic import op

revision = "0075"
down_revision = "0073"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_sessions", sa.Column("external_session_id", sa.String(length=200), nullable=True))
    op.add_column("agent_sessions", sa.Column("parent_external_session_id", sa.String(length=200), nullable=True))
    op.add_column(
        "agent_sessions",
        sa.Column(
            "parent_session_id",
            sa.Integer(),
            sa.ForeignKey("agent_sessions.id", ondelete="SET NULL", name="fk_agent_sessions_parent_session_id"),
            nullable=True,
        ),
    )
    op.add_column("agent_sessions", sa.Column("client_type", sa.String(length=16), nullable=True))
    op.add_column("agent_sessions", sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True))

    op.execute(
        "UPDATE agent_sessions SET client_type = CASE WHEN connection_hash IS NOT NULL THEN 'mcp' ELSE 'rest' END"
    )

    op.create_check_constraint(
        "ck_agent_sessions_client_type",
        "agent_sessions",
        "client_type IN ('mcp', 'rest', 'webhook')",
    )
    op.alter_column("agent_sessions", "api_key_id", existing_type=sa.Integer(), nullable=True)
    op.create_check_constraint(
        "ck_agent_sessions_api_key_unless_webhook",
        "agent_sessions",
        "api_key_id IS NOT NULL OR client_type = 'webhook'",
    )
    op.create_index(
        "ix_agent_sessions_user_external",
        "agent_sessions",
        ["user_id", "external_session_id"],
        unique=False,
    )
    op.create_index("ix_agent_sessions_parent_session_id", "agent_sessions", ["parent_session_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_agent_sessions_parent_session_id", table_name="agent_sessions")
    op.drop_index("ix_agent_sessions_user_external", table_name="agent_sessions")
    op.drop_constraint("ck_agent_sessions_api_key_unless_webhook", "agent_sessions", type_="check")
    op.execute("DELETE FROM agent_sessions WHERE api_key_id IS NULL")
    op.alter_column("agent_sessions", "api_key_id", existing_type=sa.Integer(), nullable=False)
    op.drop_constraint("ck_agent_sessions_client_type", "agent_sessions", type_="check")
    op.drop_column("agent_sessions", "ended_at")
    op.drop_column("agent_sessions", "client_type")
    op.drop_column("agent_sessions", "parent_session_id")
    op.drop_column("agent_sessions", "parent_external_session_id")
    op.drop_column("agent_sessions", "external_session_id")
