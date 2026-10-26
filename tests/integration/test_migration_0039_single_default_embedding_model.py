"""Migration 0039 keeps one default embedding model and makes the database refuse a second.

Runs Alembic in a subprocess against the test database, so it changes rows
shared by every test and is marked ``serial``. The upgrade always ends at head,
the models it inserts are removed and any default that existed before is
restored, even when an assertion fails half-way.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration, pytest.mark.serial]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_INDEX = "uq_embedding_models_single_default"

# Inserted in this order, all marked default: the local one must win although it is not the oldest.
_MODELS = [
    ("mig39-hosted", "openai", "text-embedding-3-small", 1536),
    ("mig39-local", "local", "multilingual-e5-small", 384),
    ("mig39-mock", "mock", "mock-384", 384),
]
_NAMES = [name for name, *_ in _MODELS]


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


async def _execute(sql: str, params: dict | None = None) -> None:
    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        async with engine.begin() as conn:
            await conn.execute(text(sql), params or {})
    finally:
        await engine.dispose()


async def _scalars(sql: str, params: dict | None = None) -> list:
    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        async with engine.connect() as conn:
            return list((await conn.execute(text(sql), params or {})).scalars())
    finally:
        await engine.dispose()


async def _revision() -> str:
    return (await _scalars("SELECT version_num FROM alembic_version"))[0]


async def _defaults() -> list[str]:
    return await _scalars(
        "SELECT name FROM embedding_models WHERE is_default AND name = ANY(:n) ORDER BY id", {"n": _NAMES}
    )


async def _index_exists() -> bool:
    return bool(await _scalars("SELECT 1 FROM pg_indexes WHERE indexname = :i", {"i": _INDEX}))


async def test_upgrade_keeps_the_local_default_and_downgrade_drops_the_index() -> None:
    assert await _revision() >= "0039"
    previous_defaults = await _scalars("SELECT id FROM embedding_models WHERE is_default")
    await _execute("DELETE FROM embedding_models WHERE name = ANY(:n)", {"n": _NAMES})
    await _execute("UPDATE embedding_models SET is_default = false WHERE is_default")
    try:
        _alembic("downgrade", "0039-1")
        assert await _revision() == "0038"
        assert not await _index_exists()

        for name, provider, model_name, dims in _MODELS:
            await _execute(
                "INSERT INTO embedding_models (name, provider, model_name, dimensions, is_default) "
                "VALUES (:name, :provider, :model_name, :dims, true)",
                {"name": name, "provider": provider, "model_name": model_name, "dims": dims},
            )
        assert await _defaults() == _NAMES

        _alembic("upgrade", "head")
        assert await _defaults() == ["mig39-local"]
        assert await _index_exists()
        # No row is deleted, only the flag of the others is cleared.
        assert len(await _scalars("SELECT id FROM embedding_models WHERE name = ANY(:n)", {"n": _NAMES})) == 3

        with pytest.raises(IntegrityError):
            await _execute("UPDATE embedding_models SET is_default = true WHERE name = 'mig39-mock'")

        _alembic("downgrade", "0039-1")
        assert not await _index_exists()
        await _execute("UPDATE embedding_models SET is_default = true WHERE name = 'mig39-mock'")
        assert await _defaults() == ["mig39-local", "mig39-mock"]

        # Round trip back to head cleans up again and recreates the index.
        _alembic("upgrade", "head")
        assert await _defaults() == ["mig39-local"]
        assert await _index_exists()
    finally:
        _alembic("upgrade", "head")
        await _execute("DELETE FROM embedding_models WHERE name = ANY(:n)", {"n": _NAMES})
        if previous_defaults:
            await _execute("UPDATE embedding_models SET is_default = true WHERE id = :id", {"id": previous_defaults[0]})
