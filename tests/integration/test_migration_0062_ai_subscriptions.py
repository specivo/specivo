"""Migration 0062: ai_tools (seeded) and ai_subscriptions; the downgrade drops both.

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

from specivo.core.ai_subscriptions import SEEDED_TOOLS

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


async def _seeded_tools() -> list[tuple]:
    return await _execute("SELECT slug, name, default_provider, position FROM ai_tools ORDER BY position, slug")


async def test_head_has_the_tables_and_the_seeded_tools() -> None:
    assert await _table_exists("ai_tools")
    assert await _table_exists("ai_subscriptions")
    seeded = {row[0] for row in await _seeded_tools()}
    assert {slug for slug, *_ in SEEDED_TOOLS} <= seeded
    constraints = {
        r[0] for r in await _execute("SELECT conname FROM pg_constraint WHERE conrelid = 'ai_subscriptions'::regclass")
    }
    assert {
        "ck_ai_subscriptions_amount_positive",
        "ck_ai_subscriptions_period",
        "ck_ai_subscriptions_billing_period",
        "ai_subscriptions_user_id_fkey",
        "ai_subscriptions_currency_code_fkey",
    } <= constraints
    indexes = {r[0] for r in await _execute("SELECT indexname FROM pg_indexes WHERE tablename = 'ai_subscriptions'")}
    assert "ix_ai_subscriptions_user_starts" in indexes


async def test_downgrade_drops_both_tables_and_upgrade_seeds_again() -> None:
    try:
        _alembic("downgrade", "0061")
        assert not await _table_exists("ai_tools")
        assert not await _table_exists("ai_subscriptions")
    finally:
        _alembic("upgrade", "head")
    assert await _seeded_tools() == [tuple(row) for row in SEEDED_TOOLS]
