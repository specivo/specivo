"""Migration 0061: ai_model_prices replaces the agent cost tables; downgrade restores them empty.

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

from specivo.core.ai_pricing_seed import load_history, load_snapshot

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


async def _execute(sql: str, **params) -> list:
    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(sql), params)
            return list(result.all()) if result.returns_rows else []
    finally:
        await engine.dispose()


async def _table_exists(name: str) -> bool:
    rows = await _execute("SELECT to_regclass(:name) IS NOT NULL", name=f"public.{name}")
    return bool(rows[0][0])


async def test_head_has_the_seeded_price_table_and_no_agent_cost_tables() -> None:
    assert await _table_exists("ai_model_prices")
    assert not await _table_exists("agent_token_logs")
    assert not await _table_exists("model_cost_configs")
    assert await _execute("SELECT count(*) FROM pg_extension WHERE extname = 'btree_gist'") == [(1,)]
    constraints = {
        r[0] for r in await _execute("SELECT conname FROM pg_constraint WHERE conrelid = 'ai_model_prices'::regclass")
    }
    assert {"ex_ai_model_prices_no_overlap", "ck_ai_model_prices_period", "ck_ai_model_prices_source"} <= constraints
    triggers = await _execute(
        "SELECT tgname FROM pg_trigger WHERE tgrelid = 'ai_model_prices'::regclass AND NOT tgisinternal"
    )
    assert triggers == [("ai_model_prices_immutable",)]


async def test_downgrade_restores_the_old_tables_empty_and_upgrade_seeds_again() -> None:
    try:
        _alembic("downgrade", "0060")
        assert not await _table_exists("ai_model_prices")
        assert await _table_exists("agent_token_logs")
        assert await _table_exists("model_cost_configs")
        assert await _execute("SELECT count(*) FROM agent_token_logs") == [(0,)]
        assert await _execute("SELECT count(*) FROM model_cost_configs") == [(0,)]
    finally:
        _alembic("upgrade", "head")

    assert not await _table_exists("agent_token_logs")
    snapshot = load_snapshot()
    expected = len(snapshot.prices) + len(load_history())
    rows = await _execute(
        "SELECT count(*), min(effective_from), max(effective_from), count(*) FILTER (WHERE effective_to IS NULL) "
        "FROM ai_model_prices WHERE source = 'seed'"
    )
    count, earliest, latest, open_rows = rows[0]
    assert count == expected
    assert latest == snapshot.as_of
    assert open_rows == len(snapshot.prices)
    assert earliest <= snapshot.as_of
    missing_1h = await _execute("SELECT count(*) FROM ai_model_prices WHERE cache_write_1h IS NULL")
    assert missing_1h == [(0,)]
