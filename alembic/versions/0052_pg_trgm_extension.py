"""Install the ``pg_trgm`` extension for similar-issue suggestions.

Create Date: 2026-11-04
Revision ID: 0052
Revises: 0051

The new issue form suggests issues whose subject resembles the one being
typed. Candidates come from the existing ``issues.search_vector``; they are
then ranked by ``pg_trgm``'s ``similarity()`` on the subject. No trigram
index is created: the function only runs on a short candidate list.

Like ``ltree`` and ``vector`` in 0001, the extension is required: when it
cannot be created the migration stops. ``pg_trgm`` is a trusted extension, so
a role with ``CREATE`` on the database can install it.
"""

from alembic import op

revision = "0052"
down_revision = "0051"


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")


def downgrade() -> None:
    op.execute("DROP EXTENSION IF EXISTS pg_trgm")
