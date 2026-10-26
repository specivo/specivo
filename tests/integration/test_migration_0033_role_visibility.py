"""Migration 0033 round-trips: downgrade to 0032 and upgrade back to head.

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

_COLUMNS = (
    ("roles", "users_visibility"),
    ("roles", "time_entries_visibility"),
    ("trackers", "private_by_default"),
)


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


async def _state() -> dict[str, object]:
    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        async with engine.connect() as conn:
            columns = 0
            for table, column in _COLUMNS:
                columns += await conn.scalar(
                    text("SELECT count(*) FROM information_schema.columns WHERE table_name = :t AND column_name = :c"),
                    {"t": table, "c": column},
                )
            return {
                "revision": await conn.scalar(text("SELECT version_num FROM alembic_version")),
                "columns": columns,
                "checks": await conn.scalar(
                    text(
                        "SELECT count(*) FROM pg_constraint WHERE conname IN "
                        "('ck_roles_users_visibility', 'ck_roles_time_entries_visibility')"
                    )
                ),
            }
    finally:
        await engine.dispose()


async def test_downgrade_and_upgrade_round_trip() -> None:
    before = await _state()
    assert before["revision"] >= "0033"
    assert (before["columns"], before["checks"]) == (3, 2)

    try:
        _alembic("downgrade", "0032")
        down = await _state()
        assert down["revision"] == "0032"
        assert (down["columns"], down["checks"]) == (0, 0)
    finally:
        _alembic("upgrade", "head")

    after = await _state()
    assert after == before


async def test_existing_rows_keep_todays_behaviour() -> None:
    """Every role and tracker row reads the permissive defaults after the upgrade."""
    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        async with engine.connect() as conn:
            non_member = (
                await conn.execute(
                    text("SELECT users_visibility, time_entries_visibility FROM roles WHERE builtin = 1")
                )
            ).one()
    finally:
        await engine.dispose()
    assert tuple(non_member) == ("all", "all")
