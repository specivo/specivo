"""Helpers for tests that need the HNSW vector index to answer like an exact ranking.

The index on ``chunk_embeddings`` is approximate and shared by every test
database connection. Rows other tests inserted and rolled back stay in its
graph as dead entries until a vacuum, and rows other xdist workers have not
committed yet sit in it too. A scan has to walk past all of those, and on a
graph so crowded it can return fewer or different neighbours than the exact
ranking, which made tests asserting on the index path fail at random.

:func:`rebuild_hnsw_index` drops the index and creates it again inside the
test's transaction, so the graph holds exactly the rows the test can see. That
needs an exclusive lock on ``chunk_embeddings`` until the transaction rolls
back, so a test using it must be marked ``serial``. The rollback restores the
original index.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

INDEX_NAME = "ix_chunk_embeddings_hnsw_384"


async def rebuild_hnsw_index(db: AsyncSession) -> None:
    """Recreate the HNSW index over the rows this transaction sees (and that only)."""
    index_def = (
        await db.execute(text("SELECT pg_get_indexdef(to_regclass(:name))"), {"name": INDEX_NAME})
    ).scalar_one()
    await db.execute(text(f"DROP INDEX {INDEX_NAME}"))
    await db.execute(text(index_def))
    await db.execute(text("ANALYZE chunk_embeddings"))


@asynccontextmanager
async def private_index_scans(db: AsyncSession) -> AsyncIterator[None]:
    """A clean index, which the planner takes even for the few rows a test holds."""
    await rebuild_hnsw_index(db)
    await db.execute(text("SET LOCAL enable_seqscan = off"))
    await db.execute(text("SET LOCAL enable_bitmapscan = off"))
    try:
        yield
    finally:
        await db.execute(text("SET LOCAL enable_seqscan = on"))
        await db.execute(text("SET LOCAL enable_bitmapscan = on"))
