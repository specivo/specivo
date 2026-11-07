"""Migration 0067: the price trigger lets the seed move a first period back; the downgrade restores the strict trigger.

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
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration, pytest.mark.serial]

_REPO_ROOT = Path(__file__).resolve().parents[2]

_INSERT = """
INSERT INTO ai_model_prices (provider, model, currency_code, input, output, effective_from, source)
VALUES ('migtest', 'rebase-1', 'USD', 1, 2, '2026-06-01T00:00:00Z', :source)
RETURNING id
"""


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


async def _moves_back(source: str, *, rebase: bool) -> bool:
    """Whether a row of *source* may get an earlier start; nothing is kept (the transaction rolls back)."""
    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            try:
                row_id = (await conn.execute(text(_INSERT), {"source": source})).scalar_one()
                if rebase:
                    await conn.execute(text("SELECT set_config('specivo.ai_price_rebase', 'on', true)"))
                await conn.execute(
                    text("UPDATE ai_model_prices SET effective_from = '2000-01-01T00:00:00Z' WHERE id = :id"),
                    {"id": row_id},
                )
                return True
            except DBAPIError as exc:
                assert "never edited in place" in str(exc)
                return False
            finally:
                await trans.rollback()
    finally:
        await engine.dispose()


async def test_head_allows_only_the_seed_rebase_and_the_downgrade_refuses_it() -> None:
    assert await _moves_back("seed", rebase=True)
    assert not await _moves_back("seed", rebase=False)
    assert not await _moves_back("admin", rebase=True)
    try:
        _alembic("downgrade", "0066")
        assert not await _moves_back("seed", rebase=True)
    finally:
        _alembic("upgrade", "head")
    assert await _moves_back("seed", rebase=True)
