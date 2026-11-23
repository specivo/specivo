"""Migration 0081 adds ``hermes_receivers.killed_at`` (existing receivers not killed) and round-trips.

Runs Alembic in a subprocess against the test database, so it changes schema
shared by every test and is marked ``serial``. The upgrade always ends at head,
even when an assertion fails half-way. Rows it inserts are committed for real
and deleted at the end.
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
_LOGIN = "migkill_owner"


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


async def _has_column() -> bool:
    rows = await _execute(
        "SELECT 1 FROM information_schema.columns WHERE table_name = 'hermes_receivers' AND column_name = 'killed_at'"
    )
    return bool(rows)


async def _cleanup() -> None:
    await _execute(
        "DELETE FROM hermes_receivers WHERE user_id IN (SELECT id FROM users WHERE login = :login)", login=_LOGIN
    )
    await _execute("DELETE FROM users WHERE login = :login", login=_LOGIN)


async def test_killed_at_is_added_empty_and_the_downgrade_round_trips() -> None:
    await _cleanup()
    try:
        _alembic("downgrade", "0080")
        assert not await _has_column()
        await _execute(
            "INSERT INTO users (login, email, display_name) VALUES (:login, :email, 'K')",
            login=_LOGIN,
            email=f"{_LOGIN}@example.com",
        )
        await _execute(
            "INSERT INTO hermes_receivers (user_id, name, token, secret) "
            "SELECT id, 'host', :token, 'sealed' FROM users WHERE login = :login",
            login=_LOGIN,
            token="k" * 43,
        )

        _alembic("upgrade", "0081")
        assert await _has_column()
        [(killed,)] = await _execute(
            "SELECT r.killed_at FROM hermes_receivers r JOIN users u ON u.id = r.user_id WHERE u.login = :login",
            login=_LOGIN,
        )
        assert killed is None

        _alembic("downgrade", "0080")
        assert not await _has_column()
    finally:
        _alembic("upgrade", "head")
        await _cleanup()
