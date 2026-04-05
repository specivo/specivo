"""Add composite indexes and constraints for query optimization.

- security_audit_logs: composite indexes (event_type, created_at) and (user_id, created_at)
- member_roles: unique constraint on (member_id, role_id), drop redundant member_id index
- refresh_tokens: index on expires_at for background cleanup

Revision ID: 0009
Revises: 0008
Create Date: 2026-04-05
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # security_audit_logs — composite indexes for admin listing queries
    op.create_index(
        "ix_security_audit_event_type_created",
        "security_audit_logs",
        ["event_type", "created_at"],
    )
    op.create_index(
        "ix_security_audit_user_id_created",
        "security_audit_logs",
        ["user_id", "created_at"],
    )

    # member_roles — unique constraint (also serves as composite index)
    # replaces the single-column ix_member_roles_member_id index
    op.drop_index("ix_member_roles_member_id", table_name="member_roles")
    op.create_unique_constraint(
        "uq_member_roles_member_role",
        "member_roles",
        ["member_id", "role_id"],
    )

    # refresh_tokens — index for background cleanup of expired tokens
    op.create_index(
        "ix_refresh_tokens_expires_at",
        "refresh_tokens",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_refresh_tokens_expires_at", table_name="refresh_tokens")
    op.drop_constraint("uq_member_roles_member_role", "member_roles", type_="unique")
    op.create_index("ix_member_roles_member_id", "member_roles", ["member_id"])
    op.drop_index("ix_security_audit_user_id_created", table_name="security_audit_logs")
    op.drop_index("ix_security_audit_event_type_created", table_name="security_audit_logs")
