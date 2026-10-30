"""Pull/merge requests linked to issues, and the Automation role for merge automation.

Create Date: 2026-10-30
Revision ID: 0046
Revises: 0045

New tables:

* ``pull_requests``: one row per pull/merge request of a repository
  integration, unique per (integration, number). ``provider_updated_at`` is
  the forge's own update time; an event older than the stored one is not
  applied, so events arriving out of order never move the state backwards.
* ``issue_pull_request_links``: (pull request, issue) pairs, from the title,
  the body or the source branch. ``closing`` is true when the reference used
  a fixing keyword (``Fixes KEY-1``). ``merge_handled_at`` records that merge
  automation ran for the pair, so a redelivered merge never acts twice;
  ``merge_refused_at`` that the issue already has its "could not move" note.

The ``Automation`` role is what the ``$git`` service account holds in a
project whose repository has merge automation on: view and edit issues and
add comments, and no workflow transitions until an administrator grants
them. It is not assignable, so ``$git`` never shows up as an assignee. An
existing role with that name is left as it is.

Downgrade drops the tables. The role is removed only when no membership uses
it.
"""

import sqlalchemy as sa

from alembic import op

revision = "0046"
down_revision = "0045"


def upgrade() -> None:
    op.create_table(
        "pull_requests",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "integration_id",
            sa.Integer(),
            sa.ForeignKey("repository_integrations.id", ondelete="CASCADE", name="fk_pull_requests_integration_id"),
            nullable=False,
        ),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=1000), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("state", sa.String(length=10), nullable=False),
        sa.Column("source_branch", sa.String(length=255), nullable=True),
        sa.Column("target_branch", sa.String(length=255), nullable=True),
        sa.Column("author_login", sa.String(length=255), nullable=True),
        sa.Column("merged_by_login", sa.String(length=255), nullable=True),
        sa.Column("merged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("merge_commit_sha", sa.String(length=64), nullable=True),
        sa.Column("provider_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("integration_id", "number", name="uq_pull_requests_integration_number"),
        sa.CheckConstraint("state IN ('open', 'draft', 'merged', 'closed')", name="ck_pull_requests_state"),
    )

    op.create_table(
        "issue_pull_request_links",
        sa.Column(
            "pull_request_id",
            sa.BigInteger(),
            sa.ForeignKey("pull_requests.id", ondelete="CASCADE", name="fk_issue_pull_request_links_pull_request_id"),
            primary_key=True,
        ),
        sa.Column(
            "issue_id",
            sa.Integer(),
            sa.ForeignKey("issues.id", ondelete="CASCADE", name="fk_issue_pull_request_links_issue_id"),
            primary_key=True,
        ),
        sa.Column("source", sa.String(length=10), nullable=False),
        sa.Column("closing", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("merge_handled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("merge_refused_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("source IN ('title', 'body', 'branch')", name="ck_issue_pull_request_links_source"),
    )
    op.create_index("ix_issue_pull_request_links_issue_id", "issue_pull_request_links", ["issue_id"])

    op.execute(
        """
        INSERT INTO roles (
            name, position, assignable, builtin, permissions,
            issues_visibility, users_visibility, time_entries_visibility, settings
        )
        SELECT 'Automation', COALESCE(MAX(position), 0) + 1, false, 0,
               '["view_issues", "edit_issues", "add_issue_notes"]'::jsonb,
               'all', 'members_of_visible_projects', 'own', '{}'::jsonb
        FROM roles
        ON CONFLICT (name) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM roles r
        WHERE r.name = 'Automation'
          AND NOT EXISTS (SELECT 1 FROM member_roles mr WHERE mr.role_id = r.id)
          AND NOT EXISTS (SELECT 1 FROM workflow_transitions wt WHERE wt.role_id = r.id)
        """
    )
    op.drop_index("ix_issue_pull_request_links_issue_id", table_name="issue_pull_request_links")
    op.drop_table("issue_pull_request_links")
    op.drop_table("pull_requests")
