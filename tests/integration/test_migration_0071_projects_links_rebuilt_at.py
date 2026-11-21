"""Migration 0071 adds ``projects.links_rebuilt_at``, leaves existing projects unstamped, and round-trips.

Runs Alembic in a subprocess against the test database, so it changes schema
shared by every test and is marked ``serial``. The upgrade always ends at head,
even when an assertion fails half-way. The row it inserts is committed for
real (Alembic runs on its own connection) and deleted at the end.
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
_PROJECT_KEY = "MIGLRA"


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


async def _execute(sql: str, **params) -> list:
    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(sql), params)
            return list(result.all()) if result.returns_rows else []
    finally:
        await engine.dispose()


async def _column() -> list:
    return await _execute(
        "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
        "WHERE table_name = 'projects' AND column_name = 'links_rebuilt_at'"
    )


async def test_existing_projects_stay_unstamped_and_the_downgrade_drops_the_column() -> None:
    await _execute("DELETE FROM projects WHERE key = :key", key=_PROJECT_KEY)
    try:
        _alembic("downgrade", "0070")
        assert await _column() == []
        await _execute(
            "INSERT INTO projects (name, key, identifier, path, status, is_public, settings, issue_sequence) "
            "VALUES ('Upgraded project', :key, 'miglra', 'miglra', 1, false, '{}', 0)",
            key=_PROJECT_KEY,
        )

        _alembic("upgrade", "0071")
        assert await _column() == [("timestamp with time zone", "YES", None)]
        rows = await _execute("SELECT links_rebuilt_at FROM projects WHERE key = :key", key=_PROJECT_KEY)
        assert rows == [(None,)]

        _alembic("downgrade", "0070")
        assert await _column() == []
    finally:
        _alembic("upgrade", "head")
        await _execute("DELETE FROM projects WHERE key = :key", key=_PROJECT_KEY)
