"""Migration 0043 adds ``sso_providers.deactivate_on_access_loss`` and round-trips.

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
_SLUG = "mig0043-idp"
_COLUMN = "deactivate_on_access_loss"


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


async def _column() -> tuple | None:
    rows = await _execute(
        "SELECT is_nullable, column_default FROM information_schema.columns "
        "WHERE table_name = 'sso_providers' AND column_name = :name",
        name=_COLUMN,
    )
    return tuple(rows[0]) if rows else None


async def _cleanup() -> None:
    await _execute("DELETE FROM sso_providers WHERE slug = :slug", slug=_SLUG)


async def test_existing_providers_default_to_off_and_round_trip() -> None:
    assert await _column() is not None
    await _cleanup()

    try:
        _alembic("downgrade", "0042")
        assert await _column() is None
        await _execute(
            "INSERT INTO sso_providers (slug, display_name, issuer, client_id) "
            "VALUES (:slug, 'Migration IdP', 'https://idp.example.com', 'client')",
            slug=_SLUG,
        )

        _alembic("upgrade", "0043")
        nullable, default = await _column() or (None, None)
        assert nullable == "NO"
        assert "false" in str(default)
        [(value,)] = await _execute(f"SELECT {_COLUMN} FROM sso_providers WHERE slug = :slug", slug=_SLUG)
        assert value is False

        _alembic("downgrade", "0042")
        assert await _column() is None
    finally:
        _alembic("upgrade", "head")
        await _cleanup()

    assert await _column() is not None
