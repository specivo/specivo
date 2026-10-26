"""Migration 0038 builds the HNSW index for semantic search, and its downgrade drops it.

Runs Alembic in a subprocess against the test database, so it changes the
schema every test shares and is marked ``serial``. The upgrade always ends at
head, even when an assertion fails half-way.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration, pytest.mark.serial]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_INDEX = "ix_chunk_embeddings_hnsw_384"
_STATISTICS = "st_chunk_embeddings_vector_dims"


def _alembic(*args: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=_REPO_ROOT,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


async def _scalar(sql: str, params: dict | None = None) -> object:
    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        async with engine.begin() as conn:
            return await conn.scalar(text(sql), params or {})
    finally:
        await engine.dispose()


async def _execute(sql: str, params: dict | None = None) -> None:
    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        async with engine.begin() as conn:
            await conn.execute(text(sql), params or {})
    finally:
        await engine.dispose()


async def _index_state() -> bool | None:
    """True when the index is valid, False when it exists but is invalid, None when it is missing."""
    return await _scalar("SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:name)", {"name": _INDEX})


async def _statistics_exist() -> bool:
    return bool(await _scalar("SELECT count(*) FROM pg_statistic_ext WHERE stxname = :name", {"name": _STATISTICS}))


async def test_downgrade_drops_and_upgrade_rebuilds_the_index() -> None:
    assert str(await _scalar("SELECT version_num FROM alembic_version")) >= "0038"
    try:
        _alembic("downgrade", "0038-1")
        assert await _scalar("SELECT version_num FROM alembic_version") == "0037"
        assert await _index_state() is None
        assert not await _statistics_exist()

        _alembic("upgrade", "head")
        assert await _index_state() is True
        assert await _statistics_exist()
        definition = str(await _scalar("SELECT pg_get_indexdef(to_regclass(:name))", {"name": _INDEX}))
        assert "USING hnsw (((embedding)::vector(384)) vector_cosine_ops)" in definition
        assert "WHERE (vector_dims(embedding) = 384)" in definition
    finally:
        _alembic("upgrade", "head")


async def test_upgrade_replaces_an_invalid_index_left_by_an_interrupted_build() -> None:
    try:
        _alembic("downgrade", "0038-1")
        # What an interrupted CREATE INDEX CONCURRENTLY leaves behind.
        await _execute(
            f"CREATE INDEX {_INDEX} ON chunk_embeddings USING hnsw ((embedding::vector(384)) vector_cosine_ops) "
            "WHERE vector_dims(embedding) = 384"
        )
        await _execute("UPDATE pg_index SET indisvalid = false WHERE indexrelid = to_regclass(:name)", {"name": _INDEX})
        assert await _index_state() is False

        _alembic("upgrade", "head")
        assert await _index_state() is True
    finally:
        _alembic("upgrade", "head")
