"""Migration 0075 adds external ids, client type, parent link and end time to agent sessions, and round-trips.

Existing rows keep their data: ``client_type`` is back-filled ``mcp`` for rows
with a connection, else ``rest``. The downgrade drops the new columns, deletes
the sessions that have no API key (only webhook sessions may) and makes
``api_key_id`` required again.

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
_LOGIN = "migextsess_user"
_COLUMNS = ("client_type", "ended_at", "external_session_id", "parent_external_session_id", "parent_session_id")


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


async def _cleanup() -> None:
    await _execute("DELETE FROM users WHERE login = :login", login=_LOGIN)


async def _new_columns() -> list:
    return await _execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'agent_sessions' AND column_name = ANY(:names) ORDER BY column_name",
        names=list(_COLUMNS),
    )


async def _api_key_nullable() -> str:
    [(nullable,)] = await _execute(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_name = 'agent_sessions' AND column_name = 'api_key_id'"
    )
    return str(nullable)


async def test_existing_sessions_get_a_client_type_and_the_downgrade_round_trips() -> None:
    await _cleanup()
    try:
        _alembic("downgrade", "0073")
        assert await _new_columns() == []
        await _execute(
            "INSERT INTO users (login, email, display_name) VALUES (:login, 'migextsess@example.com', 'M')",
            login=_LOGIN,
        )
        await _execute(
            "INSERT INTO api_keys (user_id, name, key_prefix, key_hash) "
            "SELECT id, 'k', 'spv_mig', 'migextsess-hash' FROM users WHERE login = :login",
            login=_LOGIN,
        )
        for conn_hash in ("a" * 64, None):
            await _execute(
                "INSERT INTO agent_sessions (api_key_id, user_id, connection_hash) "
                "SELECT k.id, u.id, :hash FROM users u JOIN api_keys k ON k.user_id = u.id WHERE u.login = :login",
                hash=conn_hash,
                login=_LOGIN,
            )

        _alembic("upgrade", "0075")
        assert [row[0] for row in await _new_columns()] == sorted(_COLUMNS)
        assert await _api_key_nullable() == "YES"
        rows = await _execute(
            "SELECT s.connection_hash IS NOT NULL, s.client_type, s.external_session_id, s.parent_session_id "
            "FROM agent_sessions s JOIN users u ON u.id = s.user_id WHERE u.login = :login ORDER BY 1",
            login=_LOGIN,
        )
        assert rows == [(False, "rest", None, None), (True, "mcp", None, None)]
        # A webhook session without a key, which the downgrade has to drop.
        await _execute(
            "INSERT INTO agent_sessions (api_key_id, user_id, client_type, external_session_id) "
            "SELECT NULL, id, 'webhook', 'sess_hook' FROM users WHERE login = :login",
            login=_LOGIN,
        )

        _alembic("downgrade", "0073")
        assert await _new_columns() == []
        assert await _api_key_nullable() == "NO"
        [(count,)] = await _execute(
            "SELECT count(*) FROM agent_sessions s JOIN users u ON u.id = s.user_id WHERE u.login = :login",
            login=_LOGIN,
        )
        assert count == 2
    finally:
        _alembic("upgrade", "head")
        await _cleanup()
