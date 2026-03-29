"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-03-22 10:00:00.000000

Squashed from 26 migrations into a single initial migration.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ==========================================================================
    # Extensions
    # ==========================================================================
    op.execute("CREATE EXTENSION IF NOT EXISTS ltree")
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # ==========================================================================
    # 1. users
    # ==========================================================================
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("login", sa.String(length=100), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("password_hash", sa.String(length=128), nullable=True),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("avatar_url", sa.String(length=512), nullable=True),
        sa.Column("language", sa.String(length=10), server_default="en", nullable=False),
        sa.Column("timezone", sa.String(length=50), server_default="UTC", nullable=False),
        sa.Column("status", sa.String(length=20), server_default="active", nullable=False),
        sa.Column("is_admin", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("is_service_account", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("failed_login_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("github_id", sa.String(length=50), nullable=True),
        sa.Column("google_id", sa.String(length=50), nullable=True),
        sa.Column(
            "preferences",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('active', 'locked', 'pending_verification', 'deactivated')",
            name="ck_users_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_users_github_id",
        "users",
        ["github_id"],
        unique=False,
        postgresql_where="github_id IS NOT NULL",
    )
    op.create_index(
        "ix_users_google_id",
        "users",
        ["google_id"],
        unique=False,
        postgresql_where="google_id IS NOT NULL",
    )
    op.create_index("ix_users_status", "users", ["status"], unique=False)
    op.create_index(
        "uq_users_email_ci",
        "users",
        [sa.literal_column("lower(email)")],
        unique=True,
    )
    op.create_index(
        "uq_users_login_ci",
        "users",
        [sa.literal_column("lower(login)")],
        unique=True,
    )

    # ==========================================================================
    # 2. refresh_tokens
    # ==========================================================================
    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("device_info", sa.String(length=255), nullable=True),
        sa.Column("ip_address", sa.String(length=45), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"], unique=False)

    # ==========================================================================
    # 3. api_keys
    # ==========================================================================
    op.create_table(
        "api_keys",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("key_prefix", sa.String(length=16), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("scopes", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("ip_allowlist", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key_hash"),
    )
    op.create_index("ix_api_keys_user_id", "api_keys", ["user_id"], unique=False)

    # ==========================================================================
    # 4. roles
    # ==========================================================================
    op.create_table(
        "roles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("position", sa.Integer(), server_default="1", nullable=False),
        sa.Column("assignable", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("builtin", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "permissions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column(
            "issues_visibility",
            sa.String(length=30),
            server_default="default",
            nullable=False,
        ),
        sa.Column(
            "settings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "issues_visibility IN ('default', 'all', 'own')",
            name="ck_roles_issues_visibility",
        ),
        sa.CheckConstraint("builtin IN (0, 1, 2)", name="ck_roles_builtin"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )

    # ==========================================================================
    # 5. projects
    # ==========================================================================
    op.create_table(
        "projects",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("identifier", sa.String(length=100), nullable=False),
        sa.Column("key", sa.String(length=10), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("parent_id", sa.Integer(), nullable=True),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("is_public", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("inherit_members", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("status", sa.Integer(), server_default="1", nullable=False),
        sa.Column("issue_sequence", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "settings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("key ~ '^[A-Z][A-Z0-9]{1,9}$'", name="ck_projects_key_format"),
        sa.CheckConstraint("status IN (1, 5, 9)", name="ck_projects_status"),
        sa.ForeignKeyConstraint(["parent_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("identifier"),
        sa.UniqueConstraint("key"),
    )
    op.create_index("ix_projects_identifier", "projects", ["identifier"])
    op.create_index("ix_projects_parent_id", "projects", ["parent_id"])
    op.execute(
        "CREATE INDEX ix_projects_path_gist ON projects "
        "USING gist (CAST(path AS ltree) gist_ltree_ops)"
    )

    # ==========================================================================
    # 6. members
    # ==========================================================================
    op.create_table(
        "members",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "project_id", name="uq_members_user_project"),
    )
    op.create_index("ix_members_user_id", "members", ["user_id"])
    op.create_index("ix_members_project_id", "members", ["project_id"])

    # ==========================================================================
    # 7. member_roles
    # ==========================================================================
    op.create_table(
        "member_roles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("member_id", sa.Integer(), nullable=False),
        sa.Column("role_id", sa.Integer(), nullable=False),
        sa.Column("inherited_from", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["member_id"], ["members.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["role_id"], ["roles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_member_roles_member_id", "member_roles", ["member_id"])
    op.create_index("ix_member_roles_role_id", "member_roles", ["role_id"])

    # ==========================================================================
    # 8. enabled_modules
    # ==========================================================================
    op.create_table(
        "enabled_modules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "name", name="uq_enabled_modules_project_name"),
    )
    op.create_index("ix_enabled_modules_project_id", "enabled_modules", ["project_id"])

    # ==========================================================================
    # 9. issue_priorities
    # ==========================================================================
    op.create_table(
        "issue_priorities",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("position", sa.Integer(), server_default="1", nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("active", sa.Boolean(), server_default="true", nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    # ==========================================================================
    # 10. issue_statuses
    # ==========================================================================
    op.create_table(
        "issue_statuses",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("is_closed", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("position", sa.Integer(), server_default="1", nullable=False),
        sa.Column("default_done_ratio", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    # ==========================================================================
    # 11. issue_categories
    # ==========================================================================
    op.create_table(
        "issue_categories",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("assigned_to_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["assigned_to_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "name", name="uq_issue_categories_project_name"),
    )
    op.create_index("ix_issue_categories_project_id", "issue_categories", ["project_id"], unique=False)

    # ==========================================================================
    # 12. trackers
    # ==========================================================================
    op.create_table(
        "trackers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("default_status_id", sa.Integer(), nullable=True),
        sa.Column("is_in_roadmap", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("position", sa.Integer(), server_default="1", nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "disabled_core_fields",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["default_status_id"], ["issue_statuses.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    # ==========================================================================
    # 13. versions
    # ==========================================================================
    op.create_table(
        "versions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=30), server_default="open", nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=True),
        sa.Column("sharing", sa.String(length=30), server_default="none", nullable=False),
        sa.Column("wiki_page_title", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('open', 'locked', 'closed')", name="ck_versions_status"
        ),
        sa.CheckConstraint(
            "sharing IN ('none', 'descendants', 'hierarchy', 'tree', 'system')",
            name="ck_versions_sharing",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_versions_project_id", "versions", ["project_id"], unique=False)

    # ==========================================================================
    # 14. issues
    # ==========================================================================
    op.create_table(
        "issues",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("project_key", sa.String(length=10), nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column("tracker_id", sa.Integer(), nullable=False),
        sa.Column("status_id", sa.Integer(), nullable=False),
        sa.Column("priority_id", sa.Integer(), nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=True),
        sa.Column("author_id", sa.Integer(), nullable=False),
        sa.Column("assigned_to_id", sa.Integer(), nullable=True),
        sa.Column("subject", sa.String(length=1024), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "issue_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("parent_id", sa.Integer(), nullable=True),
        sa.Column("root_id", sa.Integer(), nullable=True),
        sa.Column("lft", sa.Integer(), server_default="1", nullable=False),
        sa.Column("rgt", sa.Integer(), server_default="2", nullable=False),
        sa.Column("fixed_version_id", sa.Integer(), nullable=True),
        sa.Column("done_ratio", sa.Integer(), server_default="0", nullable=False),
        sa.Column("estimated_hours", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("original_estimate", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("remaining_estimate", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("closed_on", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_private", sa.Boolean(), server_default="false", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("lock_version", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["assigned_to_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["author_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["category_id"], ["issue_categories.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["fixed_version_id"],
            ["versions.id"],
            name="fk_issues_fixed_version_id",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(["parent_id"], ["issues.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["priority_id"], ["issue_priorities.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["root_id"], ["issues.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["status_id"], ["issue_statuses.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["tracker_id"], ["trackers.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_key", "sequence_number", name="uq_issue_display_key"),
    )
    op.create_index("idx_issue_display_key", "issues", ["project_key", "sequence_number"], unique=False)
    op.create_index("ix_issues_assigned_to_id", "issues", ["assigned_to_id"], unique=False)
    op.create_index("ix_issues_fixed_version_id", "issues", ["fixed_version_id"], unique=False)
    op.create_index(
        "ix_issues_metadata_gin", "issues", ["issue_metadata"], unique=False, postgresql_using="gin"
    )
    op.create_index("ix_issues_parent_id", "issues", ["parent_id"], unique=False)
    op.create_index("ix_issues_project_id", "issues", ["project_id"], unique=False)
    op.create_index("ix_issues_status_id", "issues", ["status_id"], unique=False)
    # Additional indexes added in later migrations
    op.create_index("ix_issues_project_created", "issues", ["project_id", "created_at"])
    op.create_index("ix_issues_tracker_id", "issues", ["tracker_id"])
    op.create_index("ix_issues_priority_id", "issues", ["priority_id"])
    op.create_index("ix_issues_author_id", "issues", ["author_id"])
    op.create_index("ix_issues_category_id", "issues", ["category_id"])
    op.create_index("ix_issues_updated_at", "issues", ["updated_at"])

    # ==========================================================================
    # 15. issue_relations
    # ==========================================================================
    op.create_table(
        "issue_relations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("issue_from_id", sa.Integer(), nullable=False),
        sa.Column("issue_to_id", sa.Integer(), nullable=False),
        sa.Column("relation_type", sa.String(length=30), nullable=False),
        sa.Column("delay", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["issue_from_id"],
            ["issues.id"],
            name="fk_issue_relations_from",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["issue_to_id"],
            ["issues.id"],
            name="fk_issue_relations_to",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "issue_from_id",
            "issue_to_id",
            "relation_type",
            name="uq_issue_relation",
        ),
    )
    op.create_index("ix_issue_relations_from", "issue_relations", ["issue_from_id"])
    op.create_index("ix_issue_relations_to", "issue_relations", ["issue_to_id"])

    # ==========================================================================
    # 16. journals
    # ==========================================================================
    op.create_table(
        "journals",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("issue_id", sa.Integer(), nullable=True),
        sa.Column("wiki_page_id", sa.Integer(), nullable=True),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("api_key_id", sa.Integer(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("is_private", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("reply_to_id", sa.Integer(), nullable=True),
        sa.Column("is_resolved", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("resolved_by_id", sa.Integer(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_summary", sa.Text(), nullable=True),
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("edited_by_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(issue_id IS NOT NULL)::int + (wiki_page_id IS NOT NULL)::int = 1",
            name="ck_journals_one_parent",
        ),
        sa.ForeignKeyConstraint(["api_key_id"], ["api_keys.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["edited_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["issue_id"], ["issues.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reply_to_id"], ["journals.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["resolved_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        # wiki_page_id FK added below after wiki_pages table is created
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("issue_id", "sequence", name="uq_journals_issue_sequence"),
    )
    op.create_index("idx_journals_created_at_brin", "journals", ["created_at"], postgresql_using="brin")
    op.create_index("idx_journals_project_created", "journals", ["project_id", "created_at"])
    op.create_index(op.f("ix_journals_issue_id"), "journals", ["issue_id"])
    op.create_index(op.f("ix_journals_project_id"), "journals", ["project_id"])
    op.create_index(op.f("ix_journals_user_id"), "journals", ["user_id"])
    op.create_index(op.f("ix_journals_api_key_id"), "journals", ["api_key_id"])
    op.create_index(op.f("ix_journals_reply_to_id"), "journals", ["reply_to_id"])

    # ==========================================================================
    # 17. journal_details
    # ==========================================================================
    op.create_table(
        "journal_details",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("journal_id", sa.Integer(), nullable=False),
        sa.Column("property", sa.String(length=30), nullable=False),
        sa.Column("prop_key", sa.String(length=255), nullable=False),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["journal_id"], ["journals.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_journal_details_journal_id"), "journal_details", ["journal_id"])

    # ==========================================================================
    # 18. watchers
    # ==========================================================================
    op.create_table(
        "watchers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("issue_id", sa.Integer(), nullable=True),
        sa.Column("wiki_page_id", sa.Integer(), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(issue_id IS NOT NULL)::int + (wiki_page_id IS NOT NULL)::int = 1",
            name="ck_watchers_one_parent",
        ),
        sa.ForeignKeyConstraint(["issue_id"], ["issues.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        # wiki_page_id FK added below after wiki_pages table is created
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("issue_id", "user_id", name="uq_watchers_issue_user"),
    )
    op.create_index("ix_watchers_issue_id", "watchers", ["issue_id"])
    op.create_index("ix_watchers_user_id", "watchers", ["user_id"])

    # ==========================================================================
    # 19. attachments
    # ==========================================================================
    op.create_table(
        "attachments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("container_type", sa.String(length=30), nullable=False),
        sa.Column("container_id", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("disk_filename", sa.String(length=255), nullable=False, unique=True),
        sa.Column("content_type", sa.String(length=255), nullable=True),
        sa.Column("filesize", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("author_id", sa.Integer(), nullable=False),
        sa.Column("description", sa.String(length=512), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["author_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_attachments_container", "attachments", ["container_type", "container_id"])
    op.create_index("ix_attachments_author_id", "attachments", ["author_id"])
    op.execute("CREATE INDEX ix_attachments_metadata_gin ON attachments USING gin (metadata)")

    # ==========================================================================
    # 20. settings
    # ==========================================================================
    op.create_table(
        "settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key"),
    )

    # ==========================================================================
    # 21. metadata_schemas
    # ==========================================================================
    op.create_table(
        "metadata_schemas",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("tracker_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "schema_definition",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tracker_id"], ["trackers.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id", "tracker_id", "name", name="uq_metadata_schema_project_tracker_name"
        ),
    )
    op.create_index("ix_metadata_schemas_project_id", "metadata_schemas", ["project_id"], unique=False)
    op.create_index("ix_metadata_schemas_tracker_id", "metadata_schemas", ["tracker_id"], unique=False)

    # ==========================================================================
    # 22. time_entry_activities
    # ==========================================================================
    op.create_table(
        "time_entry_activities",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("active", sa.Boolean(), server_default="true", nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_time_entry_activities_name"),
    )

    # ==========================================================================
    # 23. time_entries
    # ==========================================================================
    op.create_table(
        "time_entries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("issue_id", sa.Integer(), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("activity_id", sa.Integer(), nullable=False),
        sa.Column("hours", sa.Numeric(precision=8, scale=2), nullable=False),
        sa.Column("comments", sa.Text(), nullable=True),
        sa.Column("spent_on", sa.Date(), nullable=False),
        sa.Column("is_billable", sa.Boolean(), server_default="false", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["issue_id"], ["issues.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["activity_id"], ["time_entry_activities.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_time_entries_project_id", "time_entries", ["project_id"], unique=False)
    op.create_index("ix_time_entries_issue_id", "time_entries", ["issue_id"], unique=False)
    op.create_index("ix_time_entries_user_id", "time_entries", ["user_id"], unique=False)
    op.create_index("ix_time_entries_spent_on", "time_entries", ["spent_on"], unique=False)

    # ==========================================================================
    # 24. active_timers
    # ==========================================================================
    op.create_table(
        "active_timers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("issue_id", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("comments", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["issue_id"], ["issues.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", name="uq_active_timers_user_id"),
    )

    # ==========================================================================
    # 25. workflow_transitions
    # ==========================================================================
    op.create_table(
        "workflow_transitions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tracker_id", sa.Integer(), nullable=False),
        sa.Column("role_id", sa.Integer(), nullable=False),
        sa.Column("old_status_id", sa.Integer(), nullable=False),
        sa.Column("new_status_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["tracker_id"], ["trackers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["role_id"], ["roles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["old_status_id"], ["issue_statuses.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["new_status_id"], ["issue_statuses.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tracker_id",
            "role_id",
            "old_status_id",
            "new_status_id",
            name="uq_workflow_transition",
        ),
    )
    op.create_index(
        "ix_wf_transitions_lookup",
        "workflow_transitions",
        ["tracker_id", "role_id", "old_status_id"],
        unique=False,
    )

    # ==========================================================================
    # 26. workflow_field_rules
    # ==========================================================================
    op.create_table(
        "workflow_field_rules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tracker_id", sa.Integer(), nullable=False),
        sa.Column("role_id", sa.Integer(), nullable=False),
        sa.Column("status_id", sa.Integer(), nullable=False),
        sa.Column("field_name", sa.String(length=64), nullable=False),
        sa.Column("rule", sa.String(length=20), nullable=False),
        sa.ForeignKeyConstraint(["tracker_id"], ["trackers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["role_id"], ["roles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["status_id"], ["issue_statuses.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tracker_id",
            "role_id",
            "status_id",
            "field_name",
            name="uq_workflow_field_rule",
        ),
        sa.CheckConstraint("rule IN ('required', 'readonly')", name="ck_workflow_field_rule_type"),
    )

    # ==========================================================================
    # 27. wikis
    # ==========================================================================
    op.create_table(
        "wikis",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", name="uq_wikis_project_id"),
    )

    # ==========================================================================
    # 28. wiki_pages
    # ==========================================================================
    op.create_table(
        "wiki_pages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("wiki_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=255), nullable=False),
        sa.Column("parent_id", sa.Integer(), nullable=True),
        sa.Column("protected", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("lock_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["wiki_id"], ["wikis.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["parent_id"], ["wiki_pages.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("wiki_id", "slug", name="uq_wiki_pages_wiki_slug"),
    )
    op.create_index("ix_wiki_pages_wiki_id", "wiki_pages", ["wiki_id"])
    op.create_index("ix_wiki_pages_parent_id", "wiki_pages", ["parent_id"])

    # ==========================================================================
    # 29. wiki_contents
    # ==========================================================================
    op.create_table(
        "wiki_contents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("page_id", sa.Integer(), nullable=False),
        sa.Column("author_id", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("comments", sa.String(length=1024), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["page_id"], ["wiki_pages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["author_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("page_id", "version", name="uq_wiki_contents_page_version"),
    )
    op.create_index("ix_wiki_contents_page_id", "wiki_contents", ["page_id"])
    op.create_index("ix_wiki_contents_author_id", "wiki_contents", ["author_id"])

    # Composite index for efficient MAX(version) subquery in FTS
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_wiki_contents_page_version
            ON wiki_contents (page_id, version DESC)
        """
    )

    # ==========================================================================
    # 30. wiki_redirects
    # ==========================================================================
    op.create_table(
        "wiki_redirects",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("wiki_id", sa.Integer(), nullable=False),
        sa.Column("title_from", sa.String(length=255), nullable=False),
        sa.Column("redirected_to", sa.String(length=255), nullable=False),
        sa.ForeignKeyConstraint(["wiki_id"], ["wikis.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("wiki_id", "title_from", name="uq_wiki_redirects_from"),
    )
    op.create_index("ix_wiki_redirects_wiki_id", "wiki_redirects", ["wiki_id"])

    # -- Deferred FKs: watchers.wiki_page_id and journals.wiki_page_id ---------
    op.create_foreign_key(
        "fk_watchers_wiki_page_id",
        "watchers",
        "wiki_pages",
        ["wiki_page_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_watchers_wiki_page_id", "watchers", ["wiki_page_id"])

    op.create_foreign_key(
        "fk_journals_wiki_page_id",
        "journals",
        "wiki_pages",
        ["wiki_page_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_journals_wiki_page_id", "journals", ["wiki_page_id"])

    # ==========================================================================
    # 31. Full-text search: tsvector columns, triggers, GIN indexes
    # ==========================================================================
    # -- issues --
    op.execute("ALTER TABLE issues ADD COLUMN search_vector tsvector")
    op.execute("CREATE INDEX ix_issues_search_gin ON issues USING gin(search_vector)")
    op.execute("""
        CREATE OR REPLACE FUNCTION issues_search_vector_update() RETURNS trigger AS $$
        BEGIN
            NEW.search_vector :=
                setweight(to_tsvector('english', coalesce(NEW.subject, '')), 'A') ||
                setweight(to_tsvector('english', coalesce(NEW.description, '')), 'B');
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_issues_search_vector
            BEFORE INSERT OR UPDATE OF subject, description ON issues
            FOR EACH ROW EXECUTE FUNCTION issues_search_vector_update()
    """)

    # -- wiki_contents --
    op.execute("ALTER TABLE wiki_contents ADD COLUMN search_vector tsvector")
    op.execute(
        "CREATE INDEX ix_wiki_contents_search_gin ON wiki_contents USING gin(search_vector)"
    )
    op.execute("""
        CREATE OR REPLACE FUNCTION wiki_contents_search_vector_update() RETURNS trigger AS $$
        DECLARE
            page_title text;
        BEGIN
            SELECT title INTO page_title FROM wiki_pages WHERE id = NEW.page_id;
            NEW.search_vector :=
                setweight(to_tsvector('english', coalesce(page_title, '')), 'A') ||
                setweight(to_tsvector('english', coalesce(NEW.text, '')), 'B');
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_wiki_contents_search_vector
            BEFORE INSERT OR UPDATE OF text ON wiki_contents
            FOR EACH ROW EXECUTE FUNCTION wiki_contents_search_vector_update()
    """)

    # ==========================================================================
    # 32. saved_filters
    # ==========================================================================
    op.create_table(
        "saved_filters",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column(
            "filter_definition",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("is_public", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_saved_filters_user_id", "saved_filters", ["user_id"], unique=False)
    op.create_index("ix_saved_filters_project_id", "saved_filters", ["project_id"], unique=False)

    # ==========================================================================
    # 33. notification_preferences (with in_app_enabled from later migration)
    # ==========================================================================
    op.create_table(
        "notification_preferences",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=True),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("email_enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("in_app_enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "project_id", "event_type", name="uq_notification_pref"),
    )
    op.create_index(
        "ix_notification_preferences_user_id",
        "notification_preferences",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_notification_preferences_project_id",
        "notification_preferences",
        ["project_id"],
        unique=False,
    )

    # ==========================================================================
    # 34. agent_sessions
    # ==========================================================================
    op.create_table(
        "agent_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("api_key_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("issue_id", sa.Integer(), nullable=True),
        sa.Column("model_name", sa.String(length=100), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_activity_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["api_key_id"], ["api_keys.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["issue_id"], ["issues.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_sessions_api_key_id", "agent_sessions", ["api_key_id"], unique=False)
    op.create_index("ix_agent_sessions_user_id", "agent_sessions", ["user_id"], unique=False)
    op.create_index("ix_agent_sessions_issue_id", "agent_sessions", ["issue_id"], unique=False)

    # ==========================================================================
    # 35. webhooks
    # ==========================================================================
    op.create_table(
        "webhooks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("secret", sa.String(length=255), nullable=False),
        sa.Column("events", postgresql.JSONB(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_webhooks_project_id", "webhooks", ["project_id"], unique=False)

    # ==========================================================================
    # 36. webhook_deliveries
    # ==========================================================================
    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("webhook_id", sa.Integer(), nullable=False),
        sa.Column("event", sa.String(length=100), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("response_body", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["webhook_id"], ["webhooks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_webhook_deliveries_webhook_id", "webhook_deliveries", ["webhook_id"], unique=False
    )

    # ==========================================================================
    # 37. embedding_models
    # ==========================================================================
    op.create_table(
        "embedding_models",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("model_name", sa.String(200), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("api_key_encrypted", sa.Text(), nullable=True),
        sa.Column("passage_prefix", sa.String(500), nullable=True),
        sa.Column("query_prefix", sa.String(500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # ==========================================================================
    # 38. project_embedding_configs
    # ==========================================================================
    op.create_table(
        "project_embedding_configs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Integer(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "model_id",
            sa.Integer(),
            sa.ForeignKey("embedding_models.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("project_id", "model_id", name="uq_project_embedding_config"),
    )

    # ==========================================================================
    # 39. search_sources
    # ==========================================================================
    op.create_table(
        "search_sources",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_type", sa.String(30), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column(
            "project_id",
            sa.Integer(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("source_type", "entity_id", name="uq_search_source"),
        sa.Index("ix_search_sources_type_entity", "source_type", "entity_id"),
    )

    # ==========================================================================
    # 40. search_chunks
    # ==========================================================================
    op.create_table(
        "search_chunks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "source_id",
            sa.Integer(),
            sa.ForeignKey("search_sources.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("chunk_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("source_id", "chunk_index", name="uq_search_chunk"),
    )
    # Stored tsvector for FTS on chunk content (avoids per-query to_tsvector)
    op.execute("""
        ALTER TABLE search_chunks ADD COLUMN search_vector tsvector
        GENERATED ALWAYS AS (to_tsvector('english', content)) STORED
    """)
    op.execute("CREATE INDEX ix_search_chunks_fts ON search_chunks USING gin(search_vector)")

    # ==========================================================================
    # 41. chunk_embeddings (with pgvector)
    # ==========================================================================
    op.create_table(
        "chunk_embeddings",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "chunk_id",
            sa.Integer(),
            sa.ForeignKey("search_chunks.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "model_id",
            sa.Integer(),
            sa.ForeignKey("embedding_models.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("chunk_id", "model_id", name="uq_chunk_embedding"),
    )
    # pgvector column (no global HNSW index — per-model partial indexes are used instead)
    op.execute("ALTER TABLE chunk_embeddings ADD COLUMN embedding vector(1536) NOT NULL")

    # Partial HNSW index for default model (id=1).
    # The semantic search WHERE clause filters by model_id,
    # so PostgreSQL uses this partial index automatically.
    # Additional models get partial indexes via ensure_hnsw_index().
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_hnsw_model_1
            ON chunk_embeddings USING hnsw ((embedding::vector(1536)) vector_cosine_ops)
            WITH (m = 24, ef_construction = 128)
            WHERE model_id = 1
        """
    )

    # ==========================================================================
    # 42. notifications
    # ==========================================================================
    op.create_table(
        "notifications",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("entity_type", sa.String(length=30), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("is_read", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_notifications_user_id", "notifications", ["user_id"], unique=False)
    op.create_index("ix_notifications_project_id", "notifications", ["project_id"], unique=False)
    op.create_index(
        "ix_notifications_inbox",
        "notifications",
        ["user_id", "is_read", sa.text("created_at DESC")],
        unique=False,
    )

    # ==========================================================================
    # 43. reactions
    # ==========================================================================
    op.create_table(
        "reactions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("journal_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("emoji", sa.String(length=50), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["journal_id"], ["journals.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("journal_id", "user_id", "emoji", name="uq_reaction"),
    )
    op.create_index("ix_reactions_journal_id", "reactions", ["journal_id"], unique=False)
    op.create_index("ix_reactions_user_id", "reactions", ["user_id"], unique=False)

    # ==========================================================================
    # 44. mentions
    # ==========================================================================
    op.create_table(
        "mentions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("journal_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["journal_id"], ["journals.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("journal_id", "user_id", name="uq_mention"),
    )
    op.create_index("ix_mentions_journal_id", "mentions", ["journal_id"], unique=False)
    op.create_index("ix_mentions_user_id", "mentions", ["user_id"], unique=False)

    # ==========================================================================
    # 45. model_cost_configs
    # ==========================================================================
    op.create_table(
        "model_cost_configs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("model_name", sa.String(length=100), nullable=False),
        sa.Column("input_cost_per_1m", sa.Numeric(precision=10, scale=4), nullable=False),
        sa.Column("output_cost_per_1m", sa.Numeric(precision=10, scale=4), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "model_name", name="uq_model_cost"),
    )

    # ==========================================================================
    # 46. agent_token_logs
    # ==========================================================================
    op.create_table(
        "agent_token_logs",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("model_name", sa.String(length=100), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "cost", sa.Numeric(precision=10, scale=6), nullable=False, server_default="0"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["session_id"], ["agent_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_token_logs_session_id", "agent_token_logs", ["session_id"])

    # ==========================================================================
    # 47. billing_rates
    # ==========================================================================
    op.create_table(
        "billing_rates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("project_id", sa.Integer(), nullable=True),
        sa.Column("hourly_rate", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="USD"),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_billing_rates_user_id", "billing_rates", ["user_id"])
    op.create_index("ix_billing_rates_project_id", "billing_rates", ["project_id"])

    # ==========================================================================
    # 48. agent_groups
    # ==========================================================================
    op.create_table(
        "agent_groups",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )

    # ==========================================================================
    # 49. agent_group_memberships
    # ==========================================================================
    op.create_table(
        "agent_group_memberships",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["group_id"], ["agent_groups.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("group_id", "user_id", name="uq_agent_group_member"),
    )
    op.create_index(
        op.f("ix_agent_group_memberships_group_id"),
        "agent_group_memberships",
        ["group_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_agent_group_memberships_user_id"),
        "agent_group_memberships",
        ["user_id"],
        unique=False,
    )

    # ==========================================================================
    # 50. group_policies
    # ==========================================================================
    op.create_table(
        "group_policies",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=True),
        sa.Column("scopes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("ip_allowlist", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["group_id"], ["agent_groups.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_group_policies_group_id"), "group_policies", ["group_id"], unique=False
    )
    op.create_index(
        op.f("ix_group_policies_project_id"), "group_policies", ["project_id"], unique=False
    )

    # ==========================================================================
    # 51. external_systems
    # ==========================================================================
    op.create_table(
        "external_systems",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("system_type", sa.String(length=30), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )

    # ==========================================================================
    # 52. issued_credentials
    # ==========================================================================
    op.create_table(
        "issued_credentials",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("system_id", sa.Integer(), nullable=False),
        sa.Column("agent_user_id", sa.Integer(), nullable=False),
        sa.Column("scope", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["agent_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["system_id"], ["external_systems.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_issued_credentials_agent_user_id"),
        "issued_credentials",
        ["agent_user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_issued_credentials_system_id"),
        "issued_credentials",
        ["system_id"],
        unique=False,
    )

    # ==========================================================================
    # 53. credential_audit_logs
    # ==========================================================================
    op.create_table(
        "credential_audit_logs",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("credential_id", sa.Integer(), nullable=True),
        sa.Column("system_id", sa.Integer(), nullable=False),
        sa.Column("agent_user_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=30), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["agent_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["credential_id"], ["issued_credentials.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["system_id"], ["external_systems.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    # ==========================================================================
    # 54. kill_events
    # ==========================================================================
    op.create_table(
        "kill_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("level", sa.String(length=20), nullable=False),
        sa.Column("target_type", sa.String(length=30), nullable=True),
        sa.Column("target_id", sa.Integer(), nullable=True),
        sa.Column("triggered_by", sa.String(length=50), nullable=False),
        sa.Column("trigger_reason", sa.Text(), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    # ==========================================================================
    # 55. kill_trigger_configs
    # ==========================================================================
    op.create_table(
        "kill_trigger_configs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("metric", sa.String(length=50), nullable=False),
        sa.Column("threshold", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("cooldown_seconds", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("metric"),
    )

    # ==========================================================================
    # 56. security_audit_logs (partitioned table — raw DDL required)
    # ==========================================================================
    op.execute("""
        CREATE TABLE security_audit_logs (
            id BIGSERIAL NOT NULL,
            event_type VARCHAR(50) NOT NULL,
            user_id INTEGER,
            resource_type VARCHAR(50),
            resource_id INTEGER,
            project_id INTEGER,
            permission VARCHAR(50),
            ip_address VARCHAR(45),
            request_id VARCHAR(36),
            user_agent VARCHAR(512),
            details JSONB NOT NULL DEFAULT '{}',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (id, created_at)
        ) PARTITION BY RANGE (created_at)
    """)

    # Initial partitions
    op.execute("""
        CREATE TABLE security_audit_logs_2026_03 PARTITION OF security_audit_logs
            FOR VALUES FROM ('2026-03-01') TO ('2026-04-01')
    """)
    op.execute("""
        CREATE TABLE security_audit_logs_2026_04 PARTITION OF security_audit_logs
            FOR VALUES FROM ('2026-04-01') TO ('2026-05-01')
    """)
    op.execute("""
        CREATE TABLE security_audit_logs_default PARTITION OF security_audit_logs DEFAULT
    """)

    # Indexes on partitioned table
    op.execute("""
        CREATE INDEX idx_security_audit_created_at_brin
            ON security_audit_logs USING brin (created_at)
    """)
    op.execute("""
        CREATE INDEX ix_security_audit_event_type
            ON security_audit_logs (event_type)
    """)
    op.execute("""
        CREATE INDEX ix_security_audit_user_id
            ON security_audit_logs (user_id)
    """)
    op.execute("""
        CREATE INDEX ix_security_audit_resource
            ON security_audit_logs (resource_type, resource_id)
    """)
    op.execute("""
        CREATE INDEX ix_security_audit_project_id
            ON security_audit_logs (project_id)
    """)


def downgrade() -> None:
    # security_audit_logs (partitioned — CASCADE drops partitions)
    op.execute("DROP TABLE IF EXISTS security_audit_logs CASCADE")

    # Additional issue indexes
    op.drop_index("ix_issues_updated_at", table_name="issues")
    op.drop_index("ix_issues_category_id", table_name="issues")
    op.drop_index("ix_issues_author_id", table_name="issues")
    op.drop_index("ix_issues_priority_id", table_name="issues")
    op.drop_index("ix_issues_tracker_id", table_name="issues")
    op.drop_index("ix_issues_project_created", table_name="issues")

    # kill switch
    op.drop_table("kill_trigger_configs")
    op.drop_table("kill_events")

    # credential broker
    op.drop_table("credential_audit_logs")
    op.drop_index(op.f("ix_issued_credentials_system_id"), table_name="issued_credentials")
    op.drop_index(op.f("ix_issued_credentials_agent_user_id"), table_name="issued_credentials")
    op.drop_table("issued_credentials")
    op.drop_table("external_systems")

    # agent groups
    op.drop_index(op.f("ix_group_policies_project_id"), table_name="group_policies")
    op.drop_index(op.f("ix_group_policies_group_id"), table_name="group_policies")
    op.drop_table("group_policies")
    op.drop_index(op.f("ix_agent_group_memberships_user_id"), table_name="agent_group_memberships")
    op.drop_index(op.f("ix_agent_group_memberships_group_id"), table_name="agent_group_memberships")
    op.drop_table("agent_group_memberships")
    op.drop_table("agent_groups")

    # agent cost
    op.drop_index("ix_billing_rates_project_id", table_name="billing_rates")
    op.drop_index("ix_billing_rates_user_id", table_name="billing_rates")
    op.drop_table("billing_rates")
    op.drop_index("ix_agent_token_logs_session_id", table_name="agent_token_logs")
    op.drop_table("agent_token_logs")
    op.drop_table("model_cost_configs")

    # reactions & mentions
    op.drop_index("ix_mentions_user_id", table_name="mentions")
    op.drop_index("ix_mentions_journal_id", table_name="mentions")
    op.drop_table("mentions")
    op.drop_index("ix_reactions_user_id", table_name="reactions")
    op.drop_index("ix_reactions_journal_id", table_name="reactions")
    op.drop_table("reactions")

    # notifications
    op.drop_index("ix_notifications_inbox", table_name="notifications")
    op.drop_index("ix_notifications_project_id", table_name="notifications")
    op.drop_index("ix_notifications_user_id", table_name="notifications")
    op.drop_table("notifications")

    # search & embeddings
    op.execute("DROP INDEX IF EXISTS idx_hnsw_model_1")
    op.drop_table("chunk_embeddings")
    op.drop_table("search_chunks")
    op.drop_table("search_sources")
    op.drop_table("project_embedding_configs")
    op.drop_table("embedding_models")
    op.execute("DROP EXTENSION IF EXISTS vector")

    # webhooks
    op.drop_index("ix_webhook_deliveries_webhook_id", table_name="webhook_deliveries")
    op.drop_table("webhook_deliveries")
    op.drop_index("ix_webhooks_project_id", table_name="webhooks")
    op.drop_table("webhooks")

    # agent sessions
    op.drop_index("ix_agent_sessions_issue_id", table_name="agent_sessions")
    op.drop_index("ix_agent_sessions_user_id", table_name="agent_sessions")
    op.drop_index("ix_agent_sessions_api_key_id", table_name="agent_sessions")
    op.drop_table("agent_sessions")

    # notification preferences
    op.drop_index("ix_notification_preferences_project_id", table_name="notification_preferences")
    op.drop_index("ix_notification_preferences_user_id", table_name="notification_preferences")
    op.drop_table("notification_preferences")

    # saved filters
    op.drop_index("ix_saved_filters_project_id", table_name="saved_filters")
    op.drop_index("ix_saved_filters_user_id", table_name="saved_filters")
    op.drop_table("saved_filters")

    # FTS
    op.execute("DROP TRIGGER IF EXISTS trg_wiki_contents_search_vector ON wiki_contents")
    op.execute("DROP FUNCTION IF EXISTS wiki_contents_search_vector_update()")
    op.execute("DROP INDEX IF EXISTS ix_wiki_contents_search_gin")
    op.execute("ALTER TABLE wiki_contents DROP COLUMN IF EXISTS search_vector")
    op.execute("DROP TRIGGER IF EXISTS trg_issues_search_vector ON issues")
    op.execute("DROP FUNCTION IF EXISTS issues_search_vector_update()")
    op.execute("DROP INDEX IF EXISTS ix_issues_search_gin")
    op.execute("ALTER TABLE issues DROP COLUMN IF EXISTS search_vector")

    # Deferred wiki FKs
    op.drop_index("ix_journals_wiki_page_id", table_name="journals")
    op.drop_constraint("fk_journals_wiki_page_id", "journals", type_="foreignkey")
    op.drop_index("ix_watchers_wiki_page_id", table_name="watchers")
    op.drop_constraint("fk_watchers_wiki_page_id", "watchers", type_="foreignkey")

    # wiki
    op.drop_index("ix_wiki_redirects_wiki_id", table_name="wiki_redirects")
    op.drop_table("wiki_redirects")
    op.execute("DROP INDEX IF EXISTS idx_wiki_contents_page_version")
    op.drop_index("ix_wiki_contents_author_id", table_name="wiki_contents")
    op.drop_index("ix_wiki_contents_page_id", table_name="wiki_contents")
    op.drop_table("wiki_contents")
    op.drop_index("ix_wiki_pages_parent_id", table_name="wiki_pages")
    op.drop_index("ix_wiki_pages_wiki_id", table_name="wiki_pages")
    op.drop_table("wiki_pages")
    op.drop_table("wikis")

    # workflow
    op.drop_table("workflow_field_rules")
    op.drop_index("ix_wf_transitions_lookup", table_name="workflow_transitions")
    op.drop_table("workflow_transitions")

    # time tracking
    op.drop_table("active_timers")
    op.drop_index("ix_time_entries_spent_on", table_name="time_entries")
    op.drop_index("ix_time_entries_user_id", table_name="time_entries")
    op.drop_index("ix_time_entries_issue_id", table_name="time_entries")
    op.drop_index("ix_time_entries_project_id", table_name="time_entries")
    op.drop_table("time_entries")
    op.drop_table("time_entry_activities")

    # metadata_schemas
    op.drop_index("ix_metadata_schemas_tracker_id", table_name="metadata_schemas")
    op.drop_index("ix_metadata_schemas_project_id", table_name="metadata_schemas")
    op.drop_table("metadata_schemas")

    # settings
    op.drop_table("settings")

    # attachments
    op.execute("DROP INDEX IF EXISTS ix_attachments_metadata_gin")
    op.drop_index("ix_attachments_author_id", table_name="attachments")
    op.drop_index("ix_attachments_container", table_name="attachments")
    op.drop_table("attachments")

    # watchers
    op.drop_index("ix_watchers_user_id", table_name="watchers")
    op.drop_index("ix_watchers_issue_id", table_name="watchers")
    op.drop_table("watchers")

    # journal_details
    op.drop_index(op.f("ix_journal_details_journal_id"), table_name="journal_details")
    op.drop_table("journal_details")

    # journals
    op.drop_index(op.f("ix_journals_reply_to_id"), table_name="journals")
    op.drop_index(op.f("ix_journals_api_key_id"), table_name="journals")
    op.drop_index(op.f("ix_journals_user_id"), table_name="journals")
    op.drop_index(op.f("ix_journals_project_id"), table_name="journals")
    op.drop_index(op.f("ix_journals_issue_id"), table_name="journals")
    op.drop_index("idx_journals_project_created", table_name="journals")
    op.drop_index("idx_journals_created_at_brin", table_name="journals")
    op.drop_table("journals")

    # issue_relations
    op.drop_index("ix_issue_relations_to", table_name="issue_relations")
    op.drop_index("ix_issue_relations_from", table_name="issue_relations")
    op.drop_table("issue_relations")

    # issues
    op.drop_index("ix_issues_status_id", table_name="issues")
    op.drop_index("ix_issues_project_id", table_name="issues")
    op.drop_index("ix_issues_parent_id", table_name="issues")
    op.drop_index("ix_issues_metadata_gin", table_name="issues", postgresql_using="gin")
    op.drop_index("ix_issues_fixed_version_id", table_name="issues")
    op.drop_index("ix_issues_assigned_to_id", table_name="issues")
    op.drop_index("idx_issue_display_key", table_name="issues")
    op.drop_table("issues")

    # versions
    op.drop_index("ix_versions_project_id", table_name="versions")
    op.drop_table("versions")

    # trackers
    op.drop_table("trackers")

    # issue_categories
    op.drop_index("ix_issue_categories_project_id", table_name="issue_categories")
    op.drop_table("issue_categories")

    # issue lookups
    op.drop_table("issue_statuses")
    op.drop_table("issue_priorities")

    # enabled_modules
    op.drop_index("ix_enabled_modules_project_id", table_name="enabled_modules")
    op.drop_table("enabled_modules")

    # member_roles
    op.drop_index("ix_member_roles_role_id", table_name="member_roles")
    op.drop_index("ix_member_roles_member_id", table_name="member_roles")
    op.drop_table("member_roles")

    # members
    op.drop_index("ix_members_project_id", table_name="members")
    op.drop_index("ix_members_user_id", table_name="members")
    op.drop_table("members")

    # projects
    op.execute("DROP INDEX IF EXISTS ix_projects_path_gist")
    op.drop_index("ix_projects_parent_id", table_name="projects")
    op.drop_index("ix_projects_identifier", table_name="projects")
    op.drop_table("projects")

    # roles
    op.drop_table("roles")

    # api_keys
    op.drop_index("ix_api_keys_user_id", table_name="api_keys")
    op.drop_table("api_keys")

    # refresh_tokens
    op.drop_index("ix_refresh_tokens_user_id", table_name="refresh_tokens")
    op.drop_table("refresh_tokens")

    # users
    op.drop_index("uq_users_login_ci", table_name="users")
    op.drop_index("uq_users_email_ci", table_name="users")
    op.drop_index("ix_users_status", table_name="users")
    op.drop_index("ix_users_google_id", table_name="users", postgresql_where="google_id IS NOT NULL")
    op.drop_index("ix_users_github_id", table_name="users", postgresql_where="github_id IS NOT NULL")
    op.drop_table("users")
