"""Hermes event receivers, their accepted delivery ids, and the receiver of an agent session.

Create Date: 2026-11-23
Revision ID: 0080
Revises: 0078

Hermes Agent posts signed outbound webhooks (tool calls, approvals, subagents,
session start and end). A receiver is where one Hermes instance posts them.

- ``hermes_receivers``: ``user_id`` (the account the events belong to,
  ``ON DELETE RESTRICT``), ``name``, ``token`` (unique, the URL part),
  ``secret`` (a ``secret_box`` token, purpose ``hermes-receiver-secret``),
  ``is_active``, ``last_event_at``, ``created_by_id`` (``ON DELETE SET
  NULL``), timestamps. Indexes on both user columns.
- ``hermes_deliveries``: the delivery ids a receiver accepted, primary key
  ``(receiver_id, delivery_id)``, cascading with the receiver; index on
  ``received_at`` for the seven-day prune.
- ``agent_sessions.receiver_id`` (nullable, ``ON DELETE SET NULL``, indexed):
  the receiver that opened a webhook session.
"""

import sqlalchemy as sa

from alembic import op

revision = "0080"
down_revision = "0078"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hermes_receivers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("token", sa.String(64), nullable=False),
        sa.Column("secret", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("token", name="hermes_receivers_token_key"),
    )
    op.create_index("ix_hermes_receivers_user_id", "hermes_receivers", ["user_id"])
    op.create_index("ix_hermes_receivers_created_by_id", "hermes_receivers", ["created_by_id"])

    op.create_table(
        "hermes_deliveries",
        sa.Column(
            "receiver_id",
            sa.Integer(),
            sa.ForeignKey("hermes_receivers.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("delivery_id", sa.String(64), primary_key=True, nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_hermes_deliveries_received_at", "hermes_deliveries", ["received_at"])

    op.add_column("agent_sessions", sa.Column("receiver_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_agent_sessions_receiver_id",
        "agent_sessions",
        "hermes_receivers",
        ["receiver_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_agent_sessions_receiver_id", "agent_sessions", ["receiver_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_sessions_receiver_id", table_name="agent_sessions")
    op.drop_constraint("fk_agent_sessions_receiver_id", "agent_sessions", type_="foreignkey")
    op.drop_column("agent_sessions", "receiver_id")
    op.drop_index("ix_hermes_deliveries_received_at", table_name="hermes_deliveries")
    op.drop_table("hermes_deliveries")
    op.drop_index("ix_hermes_receivers_created_by_id", table_name="hermes_receivers")
    op.drop_index("ix_hermes_receivers_user_id", table_name="hermes_receivers")
    op.drop_table("hermes_receivers")
