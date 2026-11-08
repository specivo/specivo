"""Migration 0052 installs ``pg_trgm`` and round-trips.

Runs Alembic in a subprocess against the test database, so it changes schema
shared by every test and is marked ``serial``. The upgrade always ends at head,
even when an assertion fails half-way.
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


async def _scalar(sql: str):
    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        async with engine.begin() as conn:
            return (await conn.execute(text(sql))).scalar()
    finally:
        await engine.dispose()


async def _has_pg_trgm() -> bool:
    return bool(await _scalar("SELECT count(*) FROM pg_extension WHERE extname = 'pg_trgm'"))


async def test_head_has_pg_trgm() -> None:
    assert await _has_pg_trgm()
    assert await _scalar("SELECT similarity('a', 'a')") == 1


async def test_downgrade_drops_and_upgrade_restores_pg_trgm() -> None:
    try:
        _alembic("downgrade", "0051")
        assert not await _has_pg_trgm()
    finally:
        _alembic("upgrade", "head")
    assert await _has_pg_trgm()
    assert await _scalar("SELECT similarity('a', 'a')") == 1
