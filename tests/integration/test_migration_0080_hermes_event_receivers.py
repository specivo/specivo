"""Migration 0080 adds Hermes event receivers, their delivery ids and ``agent_sessions.receiver_id``, and round-trips.

After the upgrade a receiver row can be written, a webhook session points at
it, deleting the receiver clears the session's ``receiver_id`` and drops its
delivery ids, and a user with a receiver cannot be deleted. The downgrade
drops the column and both tables.

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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration, pytest.mark.serial]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LOGIN = "mighr_owner"


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


async def _tables() -> list[str]:
    rows = await _execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_name IN ('hermes_receivers', 'hermes_deliveries') ORDER BY table_name"
    )
    return [row[0] for row in rows]


async def _receiver_column() -> bool:
    rows = await _execute(
        "SELECT 1 FROM information_schema.columns WHERE table_name = 'agent_sessions' AND column_name = 'receiver_id'"
    )
    return bool(rows)


async def _cleanup() -> None:
    if await _tables():
        await _execute(
            "DELETE FROM agent_sessions WHERE user_id IN (SELECT id FROM users WHERE login = :login)", login=_LOGIN
        )
        await _execute(
            "DELETE FROM hermes_receivers WHERE user_id IN (SELECT id FROM users WHERE login = :login)", login=_LOGIN
        )
    await _execute("DELETE FROM users WHERE login = :login", login=_LOGIN)


async def test_receivers_deliveries_and_the_session_link_and_the_downgrade_round_trips() -> None:
    await _cleanup()
    try:
        _alembic("downgrade", "0079")
        assert await _tables() == []
        assert not await _receiver_column()

        _alembic("upgrade", "0080")
        assert await _tables() == ["hermes_deliveries", "hermes_receivers"]
        assert await _receiver_column()

        await _execute(
            "INSERT INTO users (login, email, display_name) VALUES (:login, :email, 'M')",
            login=_LOGIN,
            email=f"{_LOGIN}@example.com",
        )
        [(receiver_id,)] = await _execute(
            "INSERT INTO hermes_receivers (user_id, name, token, secret) "
            "SELECT id, 'host', :token, 'sealed' FROM users WHERE login = :login RETURNING id",
            login=_LOGIN,
            token="m" * 43,
        )
        await _execute("INSERT INTO hermes_deliveries (receiver_id, delivery_id) VALUES (:rid, 'd1')", rid=receiver_id)
        await _execute(
            "INSERT INTO agent_sessions (user_id, client_type, receiver_id, external_session_id) "
            "SELECT id, 'webhook', :rid, 'mig-sess' FROM users WHERE login = :login",
            rid=receiver_id,
            login=_LOGIN,
        )
        [(active,)] = await _execute("SELECT is_active FROM hermes_receivers WHERE id = :rid", rid=receiver_id)
        assert active is True

        with pytest.raises(IntegrityError):
            await _execute("DELETE FROM users WHERE login = :login", login=_LOGIN)
        with pytest.raises(IntegrityError):
            await _execute(
                "INSERT INTO hermes_receivers (user_id, name, token, secret) "
                "SELECT id, 'twin', :token, 'sealed' FROM users WHERE login = :login",
                login=_LOGIN,
                token="m" * 43,
            )

        await _execute("DELETE FROM hermes_receivers WHERE id = :rid", rid=receiver_id)
        assert await _execute("SELECT 1 FROM hermes_deliveries WHERE receiver_id = :rid", rid=receiver_id) == []
        [(linked,)] = await _execute(
            "SELECT s.receiver_id FROM agent_sessions s JOIN users u ON u.id = s.user_id WHERE u.login = :login",
            login=_LOGIN,
        )
        assert linked is None

        _alembic("downgrade", "0079")
        assert await _tables() == []
        assert not await _receiver_column()
        [(sessions,)] = await _execute(
            "SELECT count(*) FROM agent_sessions s JOIN users u ON u.id = s.user_id WHERE u.login = :login",
            login=_LOGIN,
        )
        assert sessions == 1
    finally:
        _alembic("upgrade", "head")
        await _cleanup()
