"""Per-project repository integrations, the $git service account, and 64-hex commit ids.

Create Date: 2026-10-30
Revision ID: 0045
Revises: 0044

New tables:

* ``repository_integrations``: a forge repository (GitHub, GitLab, Gitea,
  Forgejo) connected to a project, with its own hook URL (``public_id``) and
  a ``secret_box`` encrypted secret. Unique per (project, provider,
  lower(repo_full_name)).
* ``repository_deliveries``: webhook deliveries received, unique per
  (integration, delivery id) so a redelivery is recognised.
* ``repository_commits``: commits that referenced an issue, unique per
  (integration, sha).
* ``issue_commit_links``: (commit, issue) pairs, the key that makes each
  journal happen once.

The ``$git`` service account writes the commit journals, so they are never
attributed to the issue author. It has no password and is not an
administrator; ``ck_users_system_principal_inert`` keeps every ``$`` login
that way. An existing account that already uses the login or the
``git@specivo.invalid`` address stops the upgrade with an explanation, and so
does any other ``$`` login with a password or administrator rights (an import
could have created one), naming the accounts to fix.

The instance-wide push hook secrets (``github_webhook_secret``,
``gitlab_webhook_token`` settings rows) are deleted with the endpoints that
read them. Existing commit journals stay as they are.

The Software Development metadata preset accepted commit ids of 7-40 hex
digits, which rejects SHA-256 repositories; the pattern becomes 7-64 in the
preset and in every project schema copied from it.

Downgrade drops the tables and narrows the pattern back. The ``$git`` row is
removed unless journals still reference it; the deleted settings rows are not
restored (they held secrets for endpoints that no longer exist).
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0045"
down_revision = "0044"

_OLD_PATTERN = "^[a-f0-9]{7,40}$"
_NEW_PATTERN = "^[a-f0-9]{7,64}$"
_PATTERN_PATH = "{properties,commits,items,pattern}"


def _set_commit_pattern(old: str, new: str) -> None:
    for table in ("metadata_presets", "metadata_schemas"):
        op.get_bind().execute(
            sa.text(
                f"UPDATE {table} SET schema_definition = "
                f"jsonb_set(schema_definition, '{_PATTERN_PATH}', to_jsonb(CAST(:new AS text))) "
                f"WHERE schema_definition #>> '{_PATTERN_PATH}' = :old"
            ),
            {"old": old, "new": new},
        )


def _refuse_live_dollar_logins() -> None:
    """Name the ``$`` logins the inert CHECK would refuse, instead of letting it fail with a bare violation."""
    rows = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT id, login, password_hash IS NOT NULL AS has_password, is_admin FROM users "
                "WHERE login LIKE '$%' AND (password_hash IS NOT NULL OR is_admin) ORDER BY id"
            )
        )
        .all()
    )
    if not rows:
        return
    accounts = "; ".join(
        f"id={row.id} {row.login!r} ("
        + " and ".join(
            reason
            for reason, applies in (("has a password", row.has_password), ("is an administrator", row.is_admin))
            if applies
        )
        + ")"
        for row in rows
    )
    raise RuntimeError(
        "Logins starting with '$' are reserved for system accounts, which can have neither a password nor "
        f"administrator rights. These accounts break that rule: {accounts}. Rename each to a login without "
        "'$', or clear its password and administrator flag, then run the upgrade again."
    )


def upgrade() -> None:
    rows = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT id, login, email, is_service_account FROM users "
                "WHERE lower(login) = '$git' OR lower(email) = 'git@specivo.invalid'"
            )
        )
        .all()
    )
    # A row left behind by a downgrade (its journals kept it) is reused; anything else is a clash.
    ours = [r for r in rows if r.login == "$git" and r.email == "git@specivo.invalid" and r.is_service_account]
    for clash in rows:
        if clash not in ours:
            raise RuntimeError(
                f"User id={clash.id} (login {clash.login!r}, email {clash.email!r}) uses the login or email "
                "reserved for the git service account. Rename that account and run the upgrade again."
            )
    _refuse_live_dollar_logins()
    op.create_check_constraint(
        "ck_users_system_principal_inert",
        "users",
        "login NOT LIKE '$%' OR (password_hash IS NULL AND NOT is_admin)",
    )
    if not ours:
        op.execute(
            """
        INSERT INTO users (
            login, email, display_name, status, password_hash,
            is_admin, is_service_account, must_change_password, is_anonymous
        ) VALUES (
            '$git', 'git@specivo.invalid', 'Git', 'active', NULL,
            false, true, false, false
        )
        """
        )

    op.create_table(
        "repository_integrations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("public_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "project_id",
            sa.Integer(),
            sa.ForeignKey("projects.id", ondelete="CASCADE", name="fk_repository_integrations_project_id"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(length=20), nullable=False),
        sa.Column("repo_full_name", sa.String(length=255), nullable=False),
        sa.Column("repo_url", sa.String(length=2048), nullable=False),
        sa.Column("secret_encrypted", sa.Text(), nullable=False),
        sa.Column("default_branch", sa.String(length=255), nullable=False, server_default="main"),
        sa.Column("scope", sa.String(length=30), nullable=False, server_default="project"),
        sa.Column("settings", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("last_delivery_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_by_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL", name="fk_repository_integrations_created_by_id"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("public_id", name="uq_repository_integrations_public_id"),
        sa.CheckConstraint(
            "provider IN ('github', 'gitlab', 'gitea', 'forgejo')", name="ck_repository_integrations_provider"
        ),
        sa.CheckConstraint("scope IN ('project', 'project_and_subprojects')", name="ck_repository_integrations_scope"),
    )
    op.create_index(
        "uq_repository_integrations_project_provider_repo",
        "repository_integrations",
        ["project_id", "provider", sa.text("lower(repo_full_name)")],
        unique=True,
    )
    op.create_index("ix_repository_integrations_created_by_id", "repository_integrations", ["created_by_id"])

    op.create_table(
        "repository_deliveries",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "integration_id",
            sa.Integer(),
            sa.ForeignKey(
                "repository_integrations.id", ondelete="CASCADE", name="fk_repository_deliveries_integration_id"
            ),
            nullable=False,
        ),
        sa.Column("delivery_id", sa.String(length=100), nullable=False),
        sa.Column("event", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("integration_id", "delivery_id", name="uq_repository_deliveries_integration_delivery"),
        sa.CheckConstraint(
            "status IN ('accepted', 'duplicate', 'ignored', 'processed', 'failed')",
            name="ck_repository_deliveries_status",
        ),
    )
    op.create_index(
        "ix_repository_deliveries_integration_received",
        "repository_deliveries",
        ["integration_id", sa.text("received_at DESC")],
    )
    op.create_index(
        "ix_repository_deliveries_accepted_received",
        "repository_deliveries",
        ["received_at"],
        postgresql_where=sa.text("status = 'accepted'"),
    )
    op.create_index("ix_repository_deliveries_received_at", "repository_deliveries", ["received_at"])

    op.create_table(
        "repository_commits",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "integration_id",
            sa.Integer(),
            sa.ForeignKey(
                "repository_integrations.id", ondelete="CASCADE", name="fk_repository_commits_integration_id"
            ),
            nullable=False,
        ),
        sa.Column("sha", sa.String(length=64), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("author_name", sa.String(length=255), nullable=False),
        sa.Column("author_email", sa.String(length=255), nullable=False),
        sa.Column(
            "matched_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL", name="fk_repository_commits_matched_user_id"),
            nullable=True,
        ),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("branch", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("integration_id", "sha", name="uq_repository_commits_integration_sha"),
    )
    op.create_index(
        "ix_repository_commits_matched_user_id",
        "repository_commits",
        ["matched_user_id"],
        postgresql_where=sa.text("matched_user_id IS NOT NULL"),
    )

    op.create_table(
        "issue_commit_links",
        sa.Column(
            "commit_id",
            sa.BigInteger(),
            sa.ForeignKey("repository_commits.id", ondelete="CASCADE", name="fk_issue_commit_links_commit_id"),
            primary_key=True,
        ),
        sa.Column(
            "issue_id",
            sa.Integer(),
            sa.ForeignKey("issues.id", ondelete="CASCADE", name="fk_issue_commit_links_issue_id"),
            primary_key=True,
        ),
        sa.Column("source", sa.String(length=10), nullable=False),
        sa.Column("keyword", sa.String(length=10), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("source IN ('message', 'branch')", name="ck_issue_commit_links_source"),
        sa.CheckConstraint("keyword IS NULL OR keyword IN ('refs', 'fixes')", name="ck_issue_commit_links_keyword"),
    )
    op.create_index("ix_issue_commit_links_issue_id", "issue_commit_links", ["issue_id"])

    op.execute("DELETE FROM settings WHERE key IN ('github_webhook_secret', 'gitlab_webhook_token')")
    _set_commit_pattern(_OLD_PATTERN, _NEW_PATTERN)


def downgrade() -> None:
    _set_commit_pattern(_NEW_PATTERN, _OLD_PATTERN)
    op.drop_index("ix_issue_commit_links_issue_id", table_name="issue_commit_links")
    op.drop_table("issue_commit_links")
    op.drop_index("ix_repository_commits_matched_user_id", table_name="repository_commits")
    op.drop_table("repository_commits")
    op.drop_index("ix_repository_deliveries_received_at", table_name="repository_deliveries")
    op.drop_index("ix_repository_deliveries_accepted_received", table_name="repository_deliveries")
    op.drop_index("ix_repository_deliveries_integration_received", table_name="repository_deliveries")
    op.drop_table("repository_deliveries")
    op.drop_index("ix_repository_integrations_created_by_id", table_name="repository_integrations")
    op.drop_index("uq_repository_integrations_project_provider_repo", table_name="repository_integrations")
    op.drop_table("repository_integrations")
    # Commit journals written by $git are history: keep the row when anything still references it.
    op.execute(
        """
        DO $$
        BEGIN
            DELETE FROM users WHERE login = '$git';
        EXCEPTION WHEN foreign_key_violation THEN
            RAISE NOTICE 'Keeping the $git user: journals still reference it';
        END
        $$
        """
    )
    op.drop_constraint("ck_users_system_principal_inert", "users", type_="check")
