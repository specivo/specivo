"""Migration 0036 grants view_time_entries to the seeded Agent role, and its downgrade takes it back.

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

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration, pytest.mark.serial]

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The seeded Agent role before 0036, and a renamed copy of it that the migration must not touch.
_ROLES = {
    "Agent": ["view_issues", "log_time"],
    "Mig36 Renamed Agent": ["view_issues", "log_time"],
}


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


async def _permissions() -> dict[str, list[str]]:
    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text("SELECT name, permissions FROM roles WHERE name = ANY(:names)"), {"names": list(_ROLES)}
                )
            ).all()
    finally:
        await engine.dispose()
    return {name: sorted(perms) for name, perms in rows}


async def _revision() -> str:
    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        async with engine.connect() as conn:
            return await conn.scalar(text("SELECT version_num FROM alembic_version"))
    finally:
        await engine.dispose()


async def test_upgrade_grants_and_downgrade_removes_view_time_entries() -> None:
    assert await _revision() >= "0036"
    await _execute("DELETE FROM roles WHERE name = ANY(:names)", {"names": list(_ROLES)})
    try:
        _alembic("downgrade", "-1")
        assert await _revision() == "0033"
        for name, perms in _ROLES.items():
            await _execute(
                "INSERT INTO roles (name, position, assignable, builtin, permissions, issues_visibility, settings) "
                "VALUES (:name, 99, true, 0, CAST(:perms AS jsonb), 'default', CAST('{}' AS jsonb))",
                {"name": name, "perms": json.dumps(perms)},
            )

        _alembic("upgrade", "head")
        assert await _permissions() == {
            "Agent": ["log_time", "view_issues", "view_time_entries"],
            "Mig36 Renamed Agent": ["log_time", "view_issues"],
        }

        _alembic("downgrade", "-1")
        assert await _permissions() == {name: sorted(perms) for name, perms in _ROLES.items()}

        # Round trip back to head grants it once again, not twice.
        _alembic("upgrade", "head")
        assert (await _permissions())["Agent"] == ["log_time", "view_issues", "view_time_entries"]
    finally:
        _alembic("upgrade", "head")
        await _execute("DELETE FROM roles WHERE name = ANY(:names)", {"names": list(_ROLES)})
