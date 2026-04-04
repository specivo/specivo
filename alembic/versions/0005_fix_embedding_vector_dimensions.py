"""Change chunk_embeddings.embedding from vector(1536) to vector(384).

Also rebuild the HNSW index for the new dimensions.

Revision ID: 0005
Revises: 0004
Create Date: 2026-04-04
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop existing HNSW indexes (they're dimension-specific)
    op.execute("DROP INDEX IF EXISTS idx_hnsw_model_1")
    op.execute("DROP INDEX IF EXISTS idx_hnsw_model_2")
    op.execute("DROP INDEX IF EXISTS idx_hnsw_model_3")

    # Remove dimension constraint — accept any model's output dimensions.
    # HNSW indexes are rebuilt per-model by EmbeddingService.ensure_hnsw_index().
    op.execute("ALTER TABLE chunk_embeddings ALTER COLUMN embedding TYPE vector USING embedding::vector")


def downgrade() -> None:
    op.execute("ALTER TABLE chunk_embeddings ALTER COLUMN embedding TYPE vector(1536)")
