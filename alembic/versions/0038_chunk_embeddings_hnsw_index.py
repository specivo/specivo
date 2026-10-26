"""Add an HNSW vector index for semantic search.

Revision ID: 0038
Revises: 0037
Create Date: 2026-10-26

Semantic and hybrid search ranked every row of ``chunk_embeddings`` by cosine
distance, because no vector index existed since 0005 dropped the old one. This
creates one HNSW index (cosine distance, pgvector defaults ``m = 16``,
``ef_construction = 64``) for 384-dimensional embeddings, the size of the local
``multilingual-e5-small`` model. The column holds vectors of any size, so the
index is on the expression ``embedding::vector(384)`` and partial on
``vector_dims(embedding) = 384``: rows of another size stay out of it rather
than failing the cast. It is keyed on the size, not on a model id, because the
model row is only created after migrations run on a fresh install. The search
query repeats the same expression and predicate so the planner can use it.

Postgres keeps no statistics on ``vector_dims(embedding)`` by itself and would
guess that the predicate keeps 0.5% of the rows, which makes a plain scan look
cheaper than the index once ``model_id`` is filtered too. Extended statistics
on that expression give it the real share (all rows, while one model is used).

The table can be large on an existing install (one row per chunk), and building
an HNSW index takes a while, so it is built with ``CREATE INDEX CONCURRENTLY``
outside the migration transaction: writes carry on during the build. An earlier
build that was interrupted leaves an invalid index behind; it is dropped and
built again.

pgvector 0.5 is the first release with HNSW. On an older one the index is
skipped with a warning and search keeps working without it.
"""

import logging

from sqlalchemy import text

from alembic import context, op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

INDEX_NAME = "ix_chunk_embeddings_hnsw_384"
STATISTICS_NAME = "st_chunk_embeddings_vector_dims"


def _pgvector_has_hnsw() -> bool:
    if context.is_offline_mode():
        return True
    version = op.get_bind().scalar(text("SELECT extversion FROM pg_extension WHERE extname = 'vector'"))
    if version is None:
        return False
    parts = tuple(int(p) if p.isdigit() else 0 for p in version.split("."))
    return parts >= (0, 5)


def _index_is_invalid() -> bool:
    if context.is_offline_mode():
        return False
    return bool(
        op.get_bind().scalar(
            text("SELECT NOT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:name)"),
            {"name": INDEX_NAME},
        )
    )


def upgrade() -> None:
    if not _pgvector_has_hnsw():
        logger.warning("pgvector older than 0.5 has no HNSW index; semantic search runs without %s", INDEX_NAME)
        return
    with op.get_context().autocommit_block():
        if _index_is_invalid():
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_NAME}")
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX_NAME} "
            "ON chunk_embeddings USING hnsw ((embedding::vector(384)) vector_cosine_ops) "
            "WITH (m = 16, ef_construction = 64) "
            "WHERE vector_dims(embedding) = 384"
        )
    op.execute(f"CREATE STATISTICS IF NOT EXISTS {STATISTICS_NAME} ON (vector_dims(embedding)) FROM chunk_embeddings")
    op.execute("ANALYZE chunk_embeddings")


def downgrade() -> None:
    op.execute(f"DROP STATISTICS IF EXISTS {STATISTICS_NAME}")
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_NAME}")
