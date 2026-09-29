"""Let a public project opt in to being read without an account.

Create Date: 2026-09-15
Revision ID: 0031
Revises: 0030

``projects.anonymous_permissions`` lists what a visitor who is not signed in
may read in the project. It starts empty for every existing project and
nothing reads it at this revision, so the upgrade changes no behaviour.

The database holds the rules that must never be broken, whatever code path
writes the row:

- ``ck_projects_anonymous_permissions_allowed``: the value is a JSON array
  whose elements are drawn from ``view_issues`` and ``view_wiki``, the only
  permissions an anonymous visitor can ever hold. The ``jsonb_typeof`` guard
  is needed because ``<@`` also accepts a bare string such as
  ``'"view_issues"'``.
- ``ck_projects_anonymous_permissions_public``: only a public project can
  carry a non-empty list.

``ix_projects_anonymous_readable`` is a partial index over the projects that
have opted in, so listing them does not scan the table.

Downgrade drops the column together with its constraints and index. Any
opt-ins are lost.
"""

from alembic import op

revision = "0031"
down_revision = "0030"


def upgrade() -> None:
    op.execute("ALTER TABLE projects ADD COLUMN anonymous_permissions jsonb NOT NULL DEFAULT '[]'::jsonb")
    op.execute(
        """
        ALTER TABLE projects
            ADD CONSTRAINT ck_projects_anonymous_permissions_allowed
            CHECK (
                jsonb_typeof(anonymous_permissions) = 'array'
                AND anonymous_permissions <@ '["view_issues", "view_wiki"]'::jsonb
            )
        """
    )
    op.execute(
        """
        ALTER TABLE projects
            ADD CONSTRAINT ck_projects_anonymous_permissions_public
            CHECK (is_public OR anonymous_permissions = '[]'::jsonb)
        """
    )
    op.execute(
        "CREATE INDEX ix_projects_anonymous_readable ON projects (id) WHERE anonymous_permissions <> '[]'::jsonb"
    )


def downgrade() -> None:
    op.execute("DROP INDEX ix_projects_anonymous_readable")
    op.execute("ALTER TABLE projects DROP CONSTRAINT ck_projects_anonymous_permissions_public")
    op.execute("ALTER TABLE projects DROP CONSTRAINT ck_projects_anonymous_permissions_allowed")
    op.execute("ALTER TABLE projects DROP COLUMN anonymous_permissions")
