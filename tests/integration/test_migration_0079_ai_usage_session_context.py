"""Migration 0079 adds the parent session, unattended flag and agent profile of AI usage entries, and round-trips.

Existing entries read as attended, without a parent session or a profile; the
report index exists after the upgrade. The downgrade drops the index and the
three columns.

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
_LOGIN = "migctx_alice"
_INDEX = "ix_ai_usage_entries_project_unattended_date"


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
    await _execute(
        "DELETE FROM ai_usage_entries WHERE user_id IN (SELECT id FROM users WHERE login = :login)", login=_LOGIN
    )
    await _execute("DELETE FROM users WHERE login = :login", login=_LOGIN)


async def _columns() -> list[str]:
    rows = await _execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'ai_usage_entries' "
        "AND column_name IN ('parent_session_id', 'unattended', 'profile') ORDER BY column_name"
    )
    return [row[0] for row in rows]


async def _has_index() -> bool:
    rows = await _execute("SELECT 1 FROM pg_indexes WHERE indexname = :name", name=_INDEX)
    return bool(rows)


async def test_existing_entries_read_as_attended_and_the_downgrade_round_trips() -> None:
    await _cleanup()
    try:
        _alembic("downgrade", "0078")
        assert await _columns() == []
        assert not await _has_index()
        await _execute(
            "INSERT INTO users (login, email, display_name) VALUES (:login, :email, 'M')",
            login=_LOGIN,
            email=f"{_LOGIN}@example.com",
        )
        await _execute(
            "INSERT INTO ai_usage_entries (user_id, reporter_user_id, source, tool_slug, model, usage_date, priced_at)"
            " SELECT id, id, 'manual', 'claude-code', 'mig-ctx', DATE '2026-01-15', TIMESTAMPTZ '2026-01-15'"
            " FROM users WHERE login = :login",
            login=_LOGIN,
        )

        _alembic("upgrade", "0079")
        assert await _columns() == ["parent_session_id", "profile", "unattended"]
        assert await _has_index()
        rows = await _execute(
            "SELECT e.parent_session_id, e.unattended, e.profile FROM ai_usage_entries e "
            "JOIN users u ON u.id = e.user_id WHERE u.login = :login",
            login=_LOGIN,
        )
        assert rows == [(None, False, None)]

        _alembic("downgrade", "0078")
        assert await _columns() == []
        assert not await _has_index()
        [(kept,)] = await _execute(
            "SELECT count(*) FROM ai_usage_entries e JOIN users u ON u.id = e.user_id WHERE u.login = :login",
            login=_LOGIN,
        )
        assert kept == 1
    finally:
        _alembic("upgrade", "head")
        await _cleanup()
