"""Add email_change_tokens for self-service email changes confirmed by link.

Create Date: 2026-11-05
Revision ID: 0055
Revises: 0054

One row per requested change: the new address, the SHA-256 hash of the token
sent to it, its expiry, and when it was confirmed or stopped being valid. At
most one row per user is live (neither confirmed nor invalidated).
"""

import sqlalchemy as sa

from alembic import op

revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "email_change_tokens",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("new_email", sa.String(255), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalidation_reason", sa.String(20), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("token_hash", name="email_change_tokens_token_hash_key"),
        sa.CheckConstraint(
            "invalidation_reason IS NULL OR invalidation_reason IN ('superseded', 'cancelled', 'address_taken')",
            name="ck_email_change_tokens_invalidation_reason",
        ),
    )
    op.create_index("ix_email_change_tokens_user_id", "email_change_tokens", ["user_id"])
    op.create_index(
        "uq_email_change_tokens_user_live",
        "email_change_tokens",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("confirmed_at IS NULL AND invalidated_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_email_change_tokens_user_live", table_name="email_change_tokens")
    op.drop_index("ix_email_change_tokens_user_id", table_name="email_change_tokens")
    op.drop_table("email_change_tokens")
