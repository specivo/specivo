"""SCIM 2.0 provisioning: per-provider SCIM token and the resources an IdP manages.

Create Date: 2026-10-31
Revision ID: 0051
Revises: 0050

``sso_providers`` gains the SCIM switch (off), the SHA-256 of its bearer
token, the token's prefix and creation time, and the time of the last SCIM
request. SCIM can be on only while a token exists.

``scim_resources`` records which users and user groups each provider's SCIM
client created, under an opaque SCIM id. ``user_identities.linked_via`` gains
``scim``: an identity linked at the first OIDC sign-in of a SCIM-provisioned
account. ``users.deactivation_reason`` records why an account was deactivated
(SCIM reactivates only accounts SCIM deactivated).
"""

import sqlalchemy as sa

from alembic import op

revision = "0051"
down_revision = "0050"


def upgrade() -> None:
    op.add_column("users", sa.Column("deactivation_reason", sa.String(length=64), nullable=True))
    op.add_column("sso_providers", sa.Column("scim_enabled", sa.Boolean(), server_default="false", nullable=False))
    op.add_column("sso_providers", sa.Column("scim_token_hash", sa.String(length=64), nullable=True))
    op.add_column("sso_providers", sa.Column("scim_token_prefix", sa.String(length=16), nullable=True))
    op.add_column("sso_providers", sa.Column("scim_token_created_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("sso_providers", sa.Column("scim_last_used_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        "ck_sso_providers_scim_needs_token", "sso_providers", "NOT scim_enabled OR scim_token_hash IS NOT NULL"
    )

    op.drop_constraint("ck_user_identities_linked_via", "user_identities", type_="check")
    op.create_check_constraint(
        "ck_user_identities_linked_via",
        "user_identities",
        "linked_via IN ('jit', 'user_link', 'auto_email', 'admin', 'scim')",
    )

    op.create_table(
        "scim_resources",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider_id", sa.Integer(), nullable=False),
        sa.Column("resource_type", sa.String(length=8), nullable=False),
        sa.Column("scim_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("user_group_id", sa.Integer(), nullable=True),
        sa.Column("external_id", sa.String(length=512), nullable=True),
        sa.Column("user_name", sa.String(length=255), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["provider_id"], ["sso_providers.id"], name="fk_scim_resources_provider_id", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_scim_resources_user_id", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["user_group_id"], ["user_groups.id"], name="fk_scim_resources_user_group_id", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("scim_id", name="uq_scim_resources_scim_id"),
        sa.UniqueConstraint("user_id", name="uq_scim_resources_user_id"),
        sa.UniqueConstraint("user_group_id", name="uq_scim_resources_user_group_id"),
        sa.CheckConstraint("resource_type IN ('User', 'Group')", name="ck_scim_resources_resource_type"),
        sa.CheckConstraint(
            "(resource_type = 'User' AND user_id IS NOT NULL AND user_group_id IS NULL)"
            " OR (resource_type = 'Group' AND user_group_id IS NOT NULL AND user_id IS NULL"
            " AND deleted_at IS NULL)",
            name="ck_scim_resources_one_target",
        ),
    )
    op.create_index(
        "uq_scim_resources_provider_user_name",
        "scim_resources",
        ["provider_id", sa.text("lower(user_name)")],
        unique=True,
        postgresql_where=sa.text("resource_type = 'User' AND deleted_at IS NULL"),
    )
    op.create_index("ix_scim_resources_provider_type", "scim_resources", ["provider_id", "resource_type"])
    op.create_index(
        "ix_scim_resources_provider_external_id",
        "scim_resources",
        ["provider_id", "external_id"],
        postgresql_where=sa.text("external_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_scim_resources_provider_external_id", table_name="scim_resources")
    op.drop_index("ix_scim_resources_provider_type", table_name="scim_resources")
    op.drop_index("uq_scim_resources_provider_user_name", table_name="scim_resources")
    op.drop_table("scim_resources")

    op.execute("UPDATE user_identities SET linked_via = 'admin' WHERE linked_via = 'scim'")
    op.drop_constraint("ck_user_identities_linked_via", "user_identities", type_="check")
    op.create_check_constraint(
        "ck_user_identities_linked_via",
        "user_identities",
        "linked_via IN ('jit', 'user_link', 'auto_email', 'admin')",
    )

    op.drop_constraint("ck_sso_providers_scim_needs_token", "sso_providers", type_="check")
    op.drop_column("sso_providers", "scim_last_used_at")
    op.drop_column("sso_providers", "scim_token_created_at")
    op.drop_column("sso_providers", "scim_token_prefix")
    op.drop_column("sso_providers", "scim_token_hash")
    op.drop_column("sso_providers", "scim_enabled")
    op.drop_column("users", "deactivation_reason")
