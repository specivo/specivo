"""Migration 0059: the exchange_rates table; downgrade drops it and its settings, upgrade recreates it.

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


async def test_downgrade_drops_the_table_and_settings_and_upgrade_restores_it() -> None:
    assert await _table_exists("exchange_rates")
    await _execute(
        "INSERT INTO settings (key, value) VALUES ('exchange_rate_max_age_days', '3') "
        "ON CONFLICT (key) DO UPDATE SET value = '3'"
    )

    try:
        _alembic("downgrade", "0058")
        assert not await _table_exists("exchange_rates")
        assert await _execute("SELECT value FROM settings WHERE key = 'exchange_rate_max_age_days'") == []
        assert await _table_exists("currencies")
    finally:
        _alembic("upgrade", "head")

    assert await _table_exists("exchange_rates")
    columns = await _execute(
        "SELECT column_name, data_type, numeric_precision, numeric_scale FROM information_schema.columns "
        "WHERE table_name = 'exchange_rates' ORDER BY ordinal_position"
    )
    assert [c[0] for c in columns] == [
        "id",
        "rate_date",
        "base_code",
        "quote_code",
        "rate",
        "source",
        "created_by_id",
        "created_at",
    ]
    rate = next(c for c in columns if c[0] == "rate")
    assert (rate[1], rate[2], rate[3]) == ("numeric", 24, 12)
    indexes = {r[0] for r in await _execute("SELECT indexname FROM pg_indexes WHERE tablename = 'exchange_rates'")}
    assert {"ix_exchange_rates_pair_date", "uq_exchange_rates_date_pair_source"} <= indexes
