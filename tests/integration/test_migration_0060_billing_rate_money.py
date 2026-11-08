"""Migration 0060: billing_rates moves onto the money convention; round-trips, and stops on bad data.

Runs Alembic in a subprocess against the test database, so it changes schema
shared by every test and is marked ``serial``. The upgrade always ends at head,
even when an assertion fails half-way. Rows written here are committed, so
every test removes its own in a ``finally``: they carry no project and are
dated before 2000, which no other test writes.
"""

from __future__ import annotations

import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration, pytest.mark.serial]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_OURS = "project_id IS NULL AND effective_from < DATE '2000-01-01'"


def _alembic(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=_REPO_ROOT,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=False,
    )
    if check:
        assert result.returncode == 0, result.stdout + result.stderr
    return result


async def _execute(sql: str, **params) -> list:
    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(sql), params)
            return list(result.all()) if result.returns_rows else []
    finally:
        await engine.dispose()


async def _columns() -> dict[str, tuple]:
    rows = await _execute(
        "SELECT column_name, data_type, numeric_precision, numeric_scale, character_maximum_length "
        "FROM information_schema.columns WHERE table_name = 'billing_rates'"
    )
    return {r[0]: tuple(r[1:]) for r in rows}


async def _cleanup() -> None:
    await _execute(f"DELETE FROM billing_rates WHERE {_OURS}")


async def test_upgrade_converts_codes_and_widens_and_downgrade_restores() -> None:
    try:
        _alembic("downgrade", "0059")
        assert "currency" in await _columns()
        await _execute(
            "INSERT INTO billing_rates (hourly_rate, currency, effective_from) VALUES "
            "(1500, 'thb', '1999-01-01'), (80.50, 'USD', '1999-02-01'), (45, 'eur', '1999-03-01')"
        )

        _alembic("upgrade", "0060")
        columns = await _columns()
        assert "currency" not in columns
        assert columns["currency_code"][0] == "character"
        assert columns["currency_code"][3] == 3
        assert columns["hourly_rate"][:3] == ("numeric", 20, 8)
        rows = await _execute(f"SELECT currency_code, hourly_rate FROM billing_rates WHERE {_OURS} ORDER BY 1")
        assert [(r[0], r[1]) for r in rows] == [
            ("EUR", Decimal("45")),
            ("THB", Decimal("1500")),
            ("USD", Decimal("80.5")),
        ]
        fks = await _execute(
            "SELECT conname FROM pg_constraint WHERE conrelid = 'billing_rates'::regclass AND contype = 'f' "
            "AND conname = 'billing_rates_currency_code_fkey'"
        )
        assert len(fks) == 1
        indexes = {r[0] for r in await _execute("SELECT indexname FROM pg_indexes WHERE tablename = 'billing_rates'")}
        assert "ix_billing_rates_currency_code" in indexes

        # Three decimal places fit the new column but not the old one: the downgrade refuses to round.
        await _execute(f"UPDATE billing_rates SET hourly_rate = 12.125 WHERE {_OURS} AND currency_code = 'EUR'")
        refused = _alembic("downgrade", "0059", check=False)
        assert refused.returncode != 0
        assert "do not fit NUMERIC(10, 2)" in refused.stdout + refused.stderr
        assert "currency_code" in await _columns()
        await _execute(f"UPDATE billing_rates SET hourly_rate = 45 WHERE {_OURS} AND currency_code = 'EUR'")

        _alembic("downgrade", "0059")
        columns = await _columns()
        assert "currency_code" not in columns
        assert columns["hourly_rate"][:3] == ("numeric", 10, 2)
        rows = await _execute(f"SELECT currency, hourly_rate FROM billing_rates WHERE {_OURS} ORDER BY 1")
        assert [(r[0], r[1]) for r in rows] == [
            ("EUR", Decimal("45.00")),
            ("THB", Decimal("1500.00")),
            ("USD", Decimal("80.50")),
        ]
    finally:
        _alembic("upgrade", "head")
        await _cleanup()

    assert "currency_code" in await _columns()


async def test_an_unknown_currency_code_stops_the_upgrade() -> None:
    try:
        _alembic("downgrade", "0059")
        await _execute(
            "INSERT INTO billing_rates (hourly_rate, currency, effective_from) VALUES "
            "(10, 'ABC', '1999-01-01'), (10, 'USD', '1999-01-01')"
        )

        failed = _alembic("upgrade", "0060", check=False)
        assert failed.returncode != 0
        output = failed.stdout + failed.stderr
        assert "not ISO 4217 codes" in output
        assert "'ABC' (1 rate)" in output
        assert "currency" in await _columns()

        await _execute(f"UPDATE billing_rates SET currency = 'EUR' WHERE {_OURS} AND currency = 'ABC'")
        _alembic("upgrade", "0060")
        rows = await _execute(f"SELECT currency_code FROM billing_rates WHERE {_OURS} ORDER BY 1")
        assert [r[0] for r in rows] == ["EUR", "USD"]
    finally:
        _alembic("upgrade", "head")
        await _cleanup()
