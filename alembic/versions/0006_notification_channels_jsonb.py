"""Replace notification_preferences.email_enabled/in_app_enabled with channels JSONB.

Add notification_channel_configs table for external channel linking.

Revision ID: 0006
Revises: 0005
Create Date: 2026-04-05
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- notification_preferences: add channels JSONB, migrate data, drop booleans ---
    op.add_column(
        "notification_preferences",
        sa.Column("channels", sa.JSON(), nullable=False, server_default="{}"),
    )

    # Backfill from existing boolean columns
    op.execute(
        """
        UPDATE notification_preferences
        SET channels = jsonb_build_object(
            'email', email_enabled,
            'in_app', in_app_enabled
        )
        """
    )

    # Drop the old boolean columns
    op.drop_column("notification_preferences", "email_enabled")
    op.drop_column("notification_preferences", "in_app_enabled")

    # --- notification_channel_configs: new table ---
    op.create_table(
        "notification_channel_configs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("channel_key", sa.String(30), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column(
            "is_verified",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("user_id", "channel_key", name="uq_user_channel"),
    )


def downgrade() -> None:
    # Drop channel configs table
    op.drop_table("notification_channel_configs")

    # Restore boolean columns with defaults
    op.add_column(
        "notification_preferences",
        sa.Column("email_enabled", sa.Boolean(), nullable=False, server_default="true"),
    )
    op.add_column(
        "notification_preferences",
        sa.Column("in_app_enabled", sa.Boolean(), nullable=False, server_default="true"),
    )

    # Backfill from channels JSONB
    op.execute(
        """
        UPDATE notification_preferences
        SET email_enabled = COALESCE((channels->>'email')::boolean, true),
            in_app_enabled = COALESCE((channels->>'in_app')::boolean, true)
        """
    )

    # Drop channels column
    op.drop_column("notification_preferences", "channels")
