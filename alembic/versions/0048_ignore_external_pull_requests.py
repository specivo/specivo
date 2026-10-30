"""Repository integrations ignore pull requests from forks and outsiders unless told otherwise.

Create Date: 2026-10-30
Revision ID: 0048
Revises: 0047

``ignore_external_pull_requests`` joins the other switches in
``repository_integrations.settings``. It is on for every existing integration,
as for new ones: on a public repository anyone may open a pull request that
names issues, and each named issue would get a note.

Downgrade removes the key, which the previous release's settings schema
would refuse.
"""

from alembic import op

revision = "0048"
down_revision = "0047"


def upgrade() -> None:
    op.execute(
        """
        UPDATE repository_integrations
        SET settings = settings || '{"ignore_external_pull_requests": true}'::jsonb
        WHERE NOT settings ? 'ignore_external_pull_requests'
        """
    )


def downgrade() -> None:
    op.execute("UPDATE repository_integrations SET settings = settings - 'ignore_external_pull_requests'")
