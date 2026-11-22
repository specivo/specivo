"""Migration 0073: the Automation role moves after every other custom role.

Runs Alembic in a subprocess against the test database, so it changes rows
shared by every test and is marked ``serial``. The upgrade always ends at head,
the role it inserts is removed and Automation's position is put back, even
when an assertion fails half-way.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration, pytest.mark.serial]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_OTHER = "Mig73 Other Role"


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


async def _scalar(sql: str, params: dict | None = None) -> Any:
    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        async with engine.connect() as conn:
            return await conn.scalar(text(sql), params or {})
    finally:
        await engine.dispose()


async def test_upgrade_moves_automation_after_the_other_custom_roles() -> None:
    original = await _scalar("SELECT position FROM roles WHERE system_key = 'automation'")
    assert original is not None
    try:
        _alembic("downgrade", "0072")
        await _execute("UPDATE roles SET position = 1 WHERE system_key = 'automation'")
        await _execute(
            "INSERT INTO roles (name, position, assignable, builtin, permissions, issues_visibility, "
            "users_visibility, time_entries_visibility, settings) "
            "VALUES (:name, 7, true, 0, '[]'::jsonb, 'default', 'all', 'all', '{}'::jsonb)",
            {"name": _OTHER},
        )

        _alembic("upgrade", "head")

        highest_other = await _scalar("SELECT MAX(position) FROM roles WHERE builtin = 0 AND system_key IS NULL")
        assert highest_other >= 7
        assert await _scalar("SELECT position FROM roles WHERE system_key = 'automation'") == highest_other + 1
    finally:
        _alembic("upgrade", "head")
        await _execute("DELETE FROM roles WHERE name = :name", {"name": _OTHER})
        await _execute("UPDATE roles SET position = :p WHERE system_key = 'automation'", {"p": original})


async def test_downgrade_leaves_positions_alone() -> None:
    before = await _scalar("SELECT position FROM roles WHERE system_key = 'automation'")
    try:
        _alembic("downgrade", "0072")
        assert await _scalar("SELECT position FROM roles WHERE system_key = 'automation'") == before
    finally:
        _alembic("upgrade", "head")
