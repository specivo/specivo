"""Migration 0072: Developer gains manage_wiki only while unchanged, Automation gets its system key, and both reverse.

Runs Alembic in a subprocess against the test database, so it changes rows
shared by every test and is marked ``serial``. The upgrade always ends at head
and the roles it inserts are removed, even when an assertion fails half-way.
"""

from __future__ import annotations

import json
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

# The seeded Developer set before 0072, deliberately out of order and with a duplicate.
_SEEDED = [
    "view_issues",
    "add_issues",
    "edit_issues",
    "add_issue_notes",
    "edit_own_notes",
    "manage_issue_relations",
    "manage_subtasks",
    "view_wiki",
    "log_time",
    "view_time_entries",
    "view_ai_usage",
    "log_ai_usage",
    "view_issues",
]
_RENAMED = "Mig72 Renamed Developer"
_NAMES = ["Developer", _RENAMED]


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


async def _permissions(name: str) -> set[str]:
    perms = await _scalar("SELECT permissions FROM roles WHERE name = :name", {"name": name})
    return set(perms)


async def _insert(name: str, perms: list[str]) -> None:
    await _execute(
        "INSERT INTO roles (name, position, assignable, builtin, permissions, issues_visibility, settings) "
        "VALUES (:name, 99, true, 0, CAST(:perms AS jsonb), 'default', CAST('{}' AS jsonb))",
        {"name": name, "perms": json.dumps(perms)},
    )


async def test_upgrade_and_downgrade() -> None:
    assert await _scalar("SELECT version_num FROM alembic_version") >= "0072"
    await _execute("DELETE FROM roles WHERE name = ANY(:names)", {"names": _NAMES})
    try:
        _alembic("downgrade", "0071")
        await _insert("Developer", _SEEDED)
        await _insert(_RENAMED, _SEEDED)

        _alembic("upgrade", "0072")
        assert await _permissions("Developer") == {*_SEEDED, "manage_wiki"}
        assert await _permissions(_RENAMED) == set(_SEEDED)
        assert await _scalar("SELECT system_key FROM roles WHERE name = 'Automation'") == "automation"
        assert await _scalar("SELECT lock_version FROM roles WHERE name = 'Developer'") == 0

        # Round trip: the downgrade takes manage_wiki back, the upgrade grants it once.
        _alembic("downgrade", "0071")
        assert await _permissions("Developer") == set(_SEEDED)
        _alembic("upgrade", "0072")
        perms = await _scalar("SELECT permissions FROM roles WHERE name = 'Developer'")
        assert perms.count("manage_wiki") == 1

        # A Developer an administrator changed is left alone, both ways.
        _alembic("downgrade", "0071")
        customised = [p for p in _SEEDED if p != "log_time"]
        await _execute(
            "UPDATE roles SET permissions = CAST(:perms AS jsonb) WHERE name = 'Developer'",
            {"perms": json.dumps(customised)},
        )
        _alembic("upgrade", "0072")
        assert await _permissions("Developer") == set(customised)

        # A Developer with wiki editing granted by hand keeps it on downgrade unless it equals the seeded set.
        await _execute(
            "UPDATE roles SET permissions = CAST(:perms AS jsonb) WHERE name = 'Developer'",
            {"perms": json.dumps([*customised, "manage_wiki"])},
        )
        _alembic("downgrade", "0071")
        assert "manage_wiki" in await _permissions("Developer")
    finally:
        _alembic("upgrade", "head")
        await _execute("DELETE FROM roles WHERE name = ANY(:names)", {"names": _NAMES})
