"""Migration 0042 gives every refresh-token row a session id and round-trips.

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
_LOGIN = "mig0042_user"
_SESSION_COLUMNS = {
    "session_id",
    "auth_method",
    "user_identity_id",
    "idp_sid",
    "id_token_encrypted",
    "absolute_expires_at",
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


async def _execute(sql: str, **params) -> list:
    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(sql), params)
            return list(result.all()) if result.returns_rows else []
    finally:
        await engine.dispose()


async def _columns() -> set[str]:
    rows = await _execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'refresh_tokens'")
    return {row[0] for row in rows}


async def _indexes() -> set[str]:
    rows = await _execute("SELECT indexname FROM pg_indexes WHERE tablename = 'refresh_tokens'")
    return {row[0] for row in rows}


async def _cleanup() -> None:
    await _execute(
        "DELETE FROM refresh_tokens WHERE user_id IN (SELECT id FROM users WHERE login = :login)", login=_LOGIN
    )
    await _execute("DELETE FROM users WHERE login = :login", login=_LOGIN)


async def test_backfill_and_round_trip() -> None:
    assert _SESSION_COLUMNS <= await _columns()
    await _cleanup()

    try:
        _alembic("downgrade", "0041")
        assert not (_SESSION_COLUMNS & await _columns())

        await _execute(
            "INSERT INTO users (login, email, display_name) VALUES (:login, :email, :login)",
            login=_LOGIN,
            email=f"{_LOGIN}@example.com",
        )
        for n in range(3):
            await _execute(
                "INSERT INTO refresh_tokens (user_id, token_hash, expires_at) "
                "SELECT id, :hash, now() + interval '1 day' FROM users WHERE login = :login",
                hash=f"{n:064d}",
                login=_LOGIN,
            )

        _alembic("upgrade", "0042")
        assert _SESSION_COLUMNS <= await _columns()
        assert {"ix_refresh_tokens_session_id", "ix_refresh_tokens_user_identity_idp_sid"} <= await _indexes()

        rows = await _execute(
            "SELECT session_id, auth_method, user_identity_id, idp_sid, absolute_expires_at "
            "FROM refresh_tokens rt JOIN users u ON u.id = rt.user_id WHERE u.login = :login",
            login=_LOGIN,
        )
        assert len(rows) == 3
        # Every existing row becomes its own session.
        assert len({row[0] for row in rows}) == 3
        assert all(row[0] is not None for row in rows)
        assert all(row[1] == "password" for row in rows)
        assert all(row[2] is None and row[3] is None and row[4] is None for row in rows)

        # The auth_method CHECK refuses unknown values.
        with pytest.raises(Exception, match="ck_refresh_tokens_auth_method"):
            await _execute(
                "UPDATE refresh_tokens SET auth_method = 'magic' "
                "WHERE user_id IN (SELECT id FROM users WHERE login = :login)",
                login=_LOGIN,
            )

        _alembic("downgrade", "0041")
        assert not (_SESSION_COLUMNS & await _columns())
    finally:
        _alembic("upgrade", "head")
        await _cleanup()

    assert _SESSION_COLUMNS <= await _columns()
