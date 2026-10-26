"""Migration 0035 widens ``users.password_hash`` and round-trips.

Runs Alembic in a subprocess against the test database, so it changes schema
shared by every test and is marked ``serial``. The upgrade always ends at head,
even when an assertion fails half-way. Rows it inserts are committed for real
(Alembic runs on its own connection) and deleted at the end.
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
_LONG = "acme_long$" + "p" * 190  # 200 characters: only fits the widened column
_SHORT = "redmine_sha1$" + "s" * 93  # 106 characters, like a wrapped Redmine hash


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


async def _width() -> int:
    rows = await _execute(
        "SELECT character_maximum_length FROM information_schema.columns "
        "WHERE table_name = 'users' AND column_name = 'password_hash'"
    )
    return rows[0][0]


async def _hashes() -> dict[str, str | None]:
    rows = await _execute("SELECT login, password_hash FROM users WHERE login LIKE 'mig0035_%'")
    return dict(rows)


async def test_widen_and_round_trip() -> None:
    assert await _width() == 255
    for login, value in (("mig0035_long", _LONG), ("mig0035_short", _SHORT)):
        await _execute(
            "INSERT INTO users (login, email, display_name, password_hash) VALUES (:login, :email, :login, :value)",
            login=login,
            email=f"{login}@example.org",
            value=value,
        )

    try:
        _alembic("downgrade", "0035-1")
        assert await _width() == 128
        # A value the old column cannot hold is dropped (the account falls back
        # to password recovery); one that fits is kept.
        assert await _hashes() == {"mig0035_long": None, "mig0035_short": _SHORT}
    finally:
        _alembic("upgrade", "head")
        await _execute("DELETE FROM users WHERE login LIKE 'mig0035_%'")

    assert await _width() == 255
