"""Allow at most one default embedding model.

Revision ID: 0039
Revises: 0038
Create Date: 2026-10-26

Search and indexing use the embedding model marked ``is_default``, but nothing
stopped a second row from being marked too: registering a model with
``is_default`` left the previous default in place, and which one a query then
picked was arbitrary. This keeps a single default and adds a partial unique
index on ``is_default WHERE is_default`` so the database refuses a second one.

When several rows are marked default, the one kept is the bundled local model
(``multilingual-e5-small``), then any ``local`` model, then the oldest row.
Only the ``is_default`` flag of the others is cleared; no row or embedding is
deleted.
"""

from sqlalchemy import text

from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None

INDEX_NAME = "uq_embedding_models_single_default"


def upgrade() -> None:
    op.execute(
        text(
            """
            UPDATE embedding_models
            SET is_default = false
            WHERE is_default
              AND id <> (
                SELECT id FROM embedding_models
                WHERE is_default
                ORDER BY (provider = 'local' AND model_name = 'multilingual-e5-small') DESC,
                         (provider = 'local') DESC,
                         id
                LIMIT 1
              )
            """
        )
    )
    op.create_index(
        INDEX_NAME,
        "embedding_models",
        ["is_default"],
        unique=True,
        postgresql_where=text("is_default"),
    )


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="embedding_models")
