"""Development activity: CI runs, deployments, tags and releases, reviews, and detached links.

Create Date: 2026-11-06
Revision ID: 0058
Revises: 0057

New tables: ``ci_runs``, ``deployments``, ``repository_releases``,
``issue_release_links``, ``pull_request_reviews`` and
``development_detachments``. Commit and pull request links accept the
``manual`` source (moved to an issue by a person).

The downgrade drops the new tables and turns ``manual`` links back into
``message`` (commits) and ``title`` (pull requests) links, the closest
automatic sources.
"""

import sqlalchemy as sa

from alembic import op

revision = "0058"
down_revision = "0057"
branch_labels = None
depends_on = None

_PROVIDERS = "provider IN ('github', 'gitlab', 'gitea', 'forgejo')"


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    ]


def _integration() -> sa.Column:
    return sa.Column(
        "integration_id",
        sa.Integer,
        sa.ForeignKey("repository_integrations.id", ondelete="CASCADE"),
        nullable=False,
    )


def _replace_check(table: str, name: str, condition: str) -> None:
    op.drop_constraint(name, table, type_="check")
    op.create_check_constraint(name, table, condition)


def upgrade() -> None:
    _replace_check("issue_commit_links", "ck_issue_commit_links_source", "source IN ('message', 'branch', 'manual')")
    _replace_check(
        "issue_pull_request_links",
        "ck_issue_pull_request_links_source",
        "source IN ('title', 'body', 'branch', 'manual')",
    )

    op.create_table(
        "pull_request_reviews",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "pull_request_id", sa.BigInteger, sa.ForeignKey("pull_requests.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("reviewer_login", sa.String(255), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("url", sa.String(2048), nullable=False, server_default=""),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider_submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("pull_request_id", "reviewer_login", name="uq_pull_request_reviews_pr_reviewer"),
        sa.CheckConstraint(
            "state IN ('approved', 'changes_requested', 'commented', 'dismissed')",
            name="ck_pull_request_reviews_state",
        ),
    )

    op.create_table(
        "ci_runs",
        sa.Column("id", sa.BigInteger, primary_key=True),
        _integration(),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("run_type", sa.String(20), nullable=False),
        sa.Column("external_id", sa.BigInteger, nullable=False),
        sa.Column("name", sa.String(255), nullable=False, server_default=""),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("head_sha", sa.String(64), nullable=True),
        sa.Column("branch", sa.String(255), nullable=True),
        sa.Column("pull_request_number", sa.Integer, nullable=True),
        sa.Column("url", sa.String(2048), nullable=False, server_default=""),
        sa.Column("attempt", sa.Integer, nullable=False, server_default="1"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint(
            "integration_id", "provider", "run_type", "external_id", name="uq_ci_runs_integration_provider_type_id"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'success', 'failure', 'cancelled', 'skipped', 'neutral')",
            name="ck_ci_runs_status",
        ),
        sa.CheckConstraint(
            "run_type IN ('check_run', 'check_suite', 'workflow_run', 'workflow_job', 'pipeline', 'job')",
            name="ck_ci_runs_run_type",
        ),
        sa.CheckConstraint(_PROVIDERS, name="ck_ci_runs_provider"),
    )
    op.create_index("ix_ci_runs_integration_sha", "ci_runs", ["integration_id", "head_sha"])
    op.create_index("ix_ci_runs_integration_branch", "ci_runs", ["integration_id", "branch"])
    op.create_index("ix_ci_runs_integration_pr", "ci_runs", ["integration_id", "provider", "pull_request_number"])
    op.create_index("ix_ci_runs_integration_occurred", "ci_runs", ["integration_id", "occurred_at"])

    op.create_table(
        "deployments",
        sa.Column("id", sa.BigInteger, primary_key=True),
        _integration(),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("external_id", sa.BigInteger, nullable=False),
        sa.Column("environment", sa.String(255), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("sha", sa.String(64), nullable=True),
        sa.Column("ref", sa.String(255), nullable=True),
        sa.Column("url", sa.String(2048), nullable=False, server_default=""),
        sa.Column("provider_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deployed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("integration_id", "provider", "external_id", name="uq_deployments_integration_provider_id"),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'success', 'failure', 'cancelled', 'inactive')",
            name="ck_deployments_status",
        ),
        sa.CheckConstraint(_PROVIDERS, name="ck_deployments_provider"),
    )
    op.create_index("ix_deployments_integration_sha", "deployments", ["integration_id", "sha"])
    op.create_index("ix_deployments_integration_ref", "deployments", ["integration_id", "ref"])
    op.create_index("ix_deployments_integration_occurred", "deployments", ["integration_id", "occurred_at"])

    op.create_table(
        "repository_releases",
        sa.Column("id", sa.BigInteger, primary_key=True),
        _integration(),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("tag_name", sa.String(255), nullable=False),
        sa.Column("name", sa.String(255), nullable=False, server_default=""),
        sa.Column("sha", sa.String(64), nullable=True),
        sa.Column("url", sa.String(2048), nullable=False, server_default=""),
        sa.Column("is_release", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("integration_id", "provider", "tag_name", name="uq_repository_releases_integration_tag"),
        sa.CheckConstraint(_PROVIDERS, name="ck_repository_releases_provider"),
    )
    op.create_index(
        "ix_repository_releases_integration_occurred", "repository_releases", ["integration_id", "occurred_at"]
    )

    op.create_table(
        "issue_release_links",
        sa.Column(
            "release_id",
            sa.BigInteger,
            sa.ForeignKey("repository_releases.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("issue_id", sa.Integer, sa.ForeignKey("issues.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("version_id", sa.Integer, sa.ForeignKey("versions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "source IN ('tag_commit', 'tag_push', 'default_branch')", name="ck_issue_release_links_source"
        ),
    )
    op.create_index("ix_issue_release_links_issue_id", "issue_release_links", ["issue_id"])
    op.create_index("ix_issue_release_links_version_id", "issue_release_links", ["version_id"])

    op.create_table(
        "development_detachments",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("item_type", sa.String(20), nullable=False),
        sa.Column("item_id", sa.BigInteger, nullable=False),
        sa.Column("issue_id", sa.Integer, sa.ForeignKey("issues.id", ondelete="CASCADE"), nullable=False),
        sa.Column("detached_by_id", sa.Integer, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("item_type", "item_id", "issue_id", name="uq_development_detachments_item_issue"),
        sa.CheckConstraint("item_type IN ('commit', 'pull_request')", name="ck_development_detachments_item_type"),
    )
    op.create_index("ix_development_detachments_issue_id", "development_detachments", ["issue_id"])
    op.create_index("ix_development_detachments_detached_by_id", "development_detachments", ["detached_by_id"])


def downgrade() -> None:
    for table in (
        "development_detachments",
        "issue_release_links",
        "repository_releases",
        "deployments",
        "ci_runs",
        "pull_request_reviews",
    ):
        op.drop_table(table)
    op.execute("UPDATE issue_commit_links SET source = 'message' WHERE source = 'manual'")
    op.execute("UPDATE issue_pull_request_links SET source = 'title' WHERE source = 'manual'")
    _replace_check("issue_commit_links", "ck_issue_commit_links_source", "source IN ('message', 'branch')")
    _replace_check(
        "issue_pull_request_links", "ck_issue_pull_request_links_source", "source IN ('title', 'body', 'branch')"
    )
