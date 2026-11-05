"""Migration 0056: the currencies table, its seed, the reporting currency setting and the project column; round-trips.

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

from specivo.core.currency_data import load_iso4217

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


async def _project_columns() -> set[str]:
    rows = await _execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'projects'")
    return {row[0] for row in rows}


async def _reporting_setting() -> list:
    return await _execute("SELECT value FROM settings WHERE key = 'reporting_currency'")


async def test_downgrade_drops_and_upgrade_reseeds() -> None:
    assert await _table_exists("currencies")

    try:
        _alembic("downgrade", "0055")
        assert not await _table_exists("currencies")
        assert "reporting_currency_code" not in await _project_columns()
        assert await _reporting_setting() == []
    finally:
        _alembic("upgrade", "head")

    expected = load_iso4217()
    rows = await _execute("SELECT code, minor_units, kind, is_active FROM currencies ORDER BY code")
    assert [(r[0], r[1], r[2], r[3]) for r in rows] == [(c.code, c.minor_units, c.kind, c.is_active) for c in expected]
    assert "reporting_currency_code" in await _project_columns()
    assert await _reporting_setting() == [("USD",)]


async def test_upgrade_keeps_an_existing_reporting_currency() -> None:
    try:
        _alembic("downgrade", "0055")
        await _execute(
            "INSERT INTO settings (key, value) VALUES ('reporting_currency', 'EUR') "
            "ON CONFLICT (key) DO UPDATE SET value = 'EUR'"
        )
    finally:
        _alembic("upgrade", "head")

    try:
        assert await _reporting_setting() == [("EUR",)]
    finally:
        await _execute("UPDATE settings SET value = 'USD' WHERE key = 'reporting_currency'")
