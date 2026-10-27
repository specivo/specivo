"""Store OIDC providers, external identities and IdP group mappings.

Create Date: 2026-10-27
Revision ID: 0041
Revises: 0040

Adds the tables single sign-on persists to. Nothing reads them yet, so this
revision changes no behaviour.

- ``sso_providers``: one OIDC relying-party registration per row. The client
  secret is either a ``secret_box`` token or the name of an environment
  variable (``ck_sso_providers_single_secret_source``); list and object
  settings are JSONB with ``jsonb_typeof`` checks.
- ``user_identities``: a Specivo user linked to one ``(issuer, subject)``.
  Deleting the user removes its identities (CASCADE); a provider with linked
  identities cannot be deleted (RESTRICT). No tokens or raw claims are stored.
- ``sso_group_mappings``: an exact, case-sensitive IdP group value mapped to a
  ``user_groups`` row.
- ``user_group_members.managed_by_provider_id``: set on memberships that group
  sync created, so sync never touches memberships an admin added by hand.
- ``reject_sso_identity_for_inert_user()``: a trigger on ``user_identities``
  that refuses rows naming a service account or the anonymous user, the same
  way ``reject_anonymous_principal()`` guards memberships.

``users.github_id`` and ``users.google_id`` were placeholders for an OAuth
login that was never built. Nothing ever wrote them, so they are dropped with
their partial indexes. Downgrade recreates them empty.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0041"
down_revision = "0040"


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "sso_providers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("slug", sa.String(length=40), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("position", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("issuer", sa.String(length=512), nullable=False),
        sa.Column("discovery_url", sa.String(length=1024), nullable=True),
        sa.Column("client_id", sa.String(length=255), nullable=False),
        sa.Column(
            "token_endpoint_auth_method",
            sa.String(length=32),
            server_default="client_secret_basic",
            nullable=False,
        ),
        sa.Column("client_secret_encrypted", sa.Text(), nullable=True),
        sa.Column("client_secret_env", sa.String(length=255), nullable=True),
        sa.Column("scopes", sa.String(length=512), server_default="openid profile email", nullable=False),
        sa.Column("claim_config", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("allow_jit_provisioning", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("link_policy", sa.String(length=32), server_default="manual", nullable=False),
        sa.Column("trust_email_verified", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("allowed_email_domains", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("required_groups", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("admin_groups", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("sync_admin_flag", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("sync_groups", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("rp_logout_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("backchannel_logout_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("session_max_age_hours", sa.Integer(), server_default=sa.text("24"), nullable=False),
        sa.Column("extra_auth_params", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("discovery_cache", postgresql.JSONB(), nullable=True),
        sa.Column("discovery_fetched_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("slug", name="uq_sso_providers_slug"),
        sa.CheckConstraint("slug ~ '^[a-z0-9-]{2,40}$'", name="ck_sso_providers_slug"),
        sa.CheckConstraint(
            "client_secret_encrypted IS NULL OR client_secret_env IS NULL",
            name="ck_sso_providers_single_secret_source",
        ),
        sa.CheckConstraint(
            "token_endpoint_auth_method IN ('client_secret_basic', 'client_secret_post', 'none')",
            name="ck_sso_providers_token_endpoint_auth_method",
        ),
        sa.CheckConstraint("link_policy IN ('manual', 'auto_verified_email')", name="ck_sso_providers_link_policy"),
        sa.CheckConstraint("session_max_age_hours > 0", name="ck_sso_providers_session_max_age_hours"),
        sa.CheckConstraint(
            "jsonb_typeof(allowed_email_domains) = 'array'", name="ck_sso_providers_allowed_email_domains_array"
        ),
        sa.CheckConstraint("jsonb_typeof(required_groups) = 'array'", name="ck_sso_providers_required_groups_array"),
        sa.CheckConstraint("jsonb_typeof(admin_groups) = 'array'", name="ck_sso_providers_admin_groups_array"),
        sa.CheckConstraint("jsonb_typeof(claim_config) = 'object'", name="ck_sso_providers_claim_config_object"),
        sa.CheckConstraint(
            "jsonb_typeof(extra_auth_params) = 'object'", name="ck_sso_providers_extra_auth_params_object"
        ),
    )

    op.create_table(
        "user_identities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider_id", sa.Integer(), sa.ForeignKey("sso_providers.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("issuer", sa.String(length=512), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("email_at_last_login", sa.String(length=255), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("linked_via", sa.String(length=20), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("issuer", "subject", name="uq_user_identities_issuer_subject"),
        sa.UniqueConstraint("provider_id", "user_id", name="uq_user_identities_provider_user"),
        sa.CheckConstraint(
            "linked_via IN ('jit', 'user_link', 'auto_email', 'admin')", name="ck_user_identities_linked_via"
        ),
    )
    op.create_index("ix_user_identities_user_id", "user_identities", ["user_id"])
    op.create_index("ix_user_identities_provider_id", "user_identities", ["provider_id"])

    op.create_table(
        "sso_group_mappings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider_id", sa.Integer(), sa.ForeignKey("sso_providers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("idp_group", sa.String(length=512), nullable=False),
        sa.Column("user_group_id", sa.Integer(), sa.ForeignKey("user_groups.id", ondelete="CASCADE"), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint(
            "provider_id", "idp_group", "user_group_id", name="uq_sso_group_mappings_provider_group_user_group"
        ),
    )
    op.create_index("ix_sso_group_mappings_provider_id", "sso_group_mappings", ["provider_id"])
    op.create_index("ix_sso_group_mappings_user_group_id", "sso_group_mappings", ["user_group_id"])

    op.add_column(
        "user_group_members",
        sa.Column(
            "managed_by_provider_id",
            sa.Integer(),
            sa.ForeignKey("sso_providers.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_user_group_members_managed_by_provider_id",
        "user_group_members",
        ["managed_by_provider_id"],
        postgresql_where=sa.text("managed_by_provider_id IS NOT NULL"),
    )

    op.execute(
        """
        CREATE FUNCTION reject_sso_identity_for_inert_user() RETURNS trigger AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM users
                WHERE id = NEW.user_id AND (is_service_account OR is_anonymous)
            ) THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'check_violation',
                    CONSTRAINT = 'reject_sso_identity_for_inert_user',
                    MESSAGE = 'service accounts and the anonymous user cannot hold an SSO identity '
                              '(reject_sso_identity_for_inert_user)';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_user_identities_reject_inert_user
            BEFORE INSERT OR UPDATE OF user_id ON user_identities
            FOR EACH ROW EXECUTE FUNCTION reject_sso_identity_for_inert_user()
        """
    )

    op.drop_index("ix_users_github_id", table_name="users")
    op.drop_index("ix_users_google_id", table_name="users")
    op.drop_column("users", "github_id")
    op.drop_column("users", "google_id")


def downgrade() -> None:
    op.add_column("users", sa.Column("google_id", sa.String(length=50), nullable=True))
    op.add_column("users", sa.Column("github_id", sa.String(length=50), nullable=True))
    op.create_index("ix_users_google_id", "users", ["google_id"], postgresql_where=sa.text("google_id IS NOT NULL"))
    op.create_index("ix_users_github_id", "users", ["github_id"], postgresql_where=sa.text("github_id IS NOT NULL"))

    op.execute("DROP TRIGGER trg_user_identities_reject_inert_user ON user_identities")
    op.execute("DROP FUNCTION reject_sso_identity_for_inert_user()")

    op.drop_index("ix_user_group_members_managed_by_provider_id", table_name="user_group_members")
    op.drop_column("user_group_members", "managed_by_provider_id")

    op.drop_index("ix_sso_group_mappings_user_group_id", table_name="sso_group_mappings")
    op.drop_index("ix_sso_group_mappings_provider_id", table_name="sso_group_mappings")
    op.drop_table("sso_group_mappings")
    op.drop_index("ix_user_identities_provider_id", table_name="user_identities")
    op.drop_index("ix_user_identities_user_id", table_name="user_identities")
    op.drop_table("user_identities")
    op.drop_table("sso_providers")
