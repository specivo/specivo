"""Migration 0069 moves AI usage entries to the project their issue now lives in.

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
_SOURCE_KEY = "MIGAIA"
_TARGET_KEY = "MIGAIB"
_NAME = "Migration 0069"
_LOGIN = "migaimove_user"


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
    await _execute("DELETE FROM projects WHERE key IN (:a, :b)", a=_SOURCE_KEY, b=_TARGET_KEY)
    for table in ("trackers", "issue_statuses", "issue_priorities"):
        await _execute(f"DELETE FROM {table} WHERE name = :name", name=_NAME)
    await _execute("DELETE FROM users WHERE login = :login", login=_LOGIN)


async def _seed() -> None:
    for key, identifier in ((_SOURCE_KEY, "migaia"), (_TARGET_KEY, "migaib")):
        await _execute(
            "INSERT INTO projects (name, key, identifier, path, status, is_public, settings, issue_sequence) "
            "VALUES (:name, :key, :identifier, :identifier_path, 1, false, '{}', 0)",
            name=key,
            key=key,
            identifier=identifier,
            identifier_path=identifier,
        )
    await _execute(
        "INSERT INTO users (login, email, display_name) VALUES (:login, 'migaimove@example.com', 'M')", login=_LOGIN
    )
    for table in ("trackers", "issue_statuses", "issue_priorities"):
        await _execute(f"INSERT INTO {table} (name) VALUES (:name)", name=_NAME)
    # One issue that already moved to the target project, one that stayed in the source.
    for key, subject in ((_TARGET_KEY, "Moved"), (_SOURCE_KEY, "Stayed")):
        await _execute(
            "INSERT INTO issues (project_id, project_key, sequence_number, tracker_id, status_id, priority_id, "
            "author_id, subject, lock_version) "
            "SELECT p.id, p.key, 1, t.id, s.id, pr.id, u.id, :subject, 0 "
            "FROM projects p, trackers t, issue_statuses s, issue_priorities pr, users u "
            "WHERE p.key = :key AND t.name = :name AND s.name = :name AND pr.name = :name AND u.login = :login",
            key=key,
            name=_NAME,
            subject=subject,
            login=_LOGIN,
        )
    # Entries all logged in the source project: on the moved issue (stale), on the issue that
    # stayed, and on no issue.
    for model, subject in (("on-moved", "Moved"), ("on-stayed", "Stayed"), ("project-only", None)):
        await _execute(
            "INSERT INTO ai_usage_entries (project_id, issue_id, user_id, reporter_user_id, source, "
            "tool_slug, model, usage_date, priced_at) "
            "SELECT p.id, (SELECT i.id FROM issues i WHERE i.subject = :subject AND i.project_key IN (:a, :b)), "
            "u.id, u.id, 'manual', 'claude-code', :model, DATE '2026-01-15', now() "
            "FROM projects p, users u WHERE p.key = :a AND u.login = :login",
            a=_SOURCE_KEY,
            b=_TARGET_KEY,
            subject=subject,
            model=model,
            login=_LOGIN,
        )


async def test_backfill_moves_entries_to_their_issues_project() -> None:
    await _cleanup()
    try:
        _alembic("downgrade", "0068")
        await _seed()

        _alembic("upgrade", "0069")
        rows = await _execute(
            "SELECT e.model, p.key FROM ai_usage_entries e JOIN projects p ON p.id = e.project_id "
            "JOIN users u ON u.id = e.user_id WHERE u.login = :login ORDER BY e.model",
            login=_LOGIN,
        )
        assert rows == [("on-moved", _TARGET_KEY), ("on-stayed", _SOURCE_KEY), ("project-only", _SOURCE_KEY)]

        # The downgrade leaves the corrected data alone.
        _alembic("downgrade", "0068")
        moved = await _execute(
            "SELECT p.key FROM ai_usage_entries e JOIN projects p ON p.id = e.project_id "
            "WHERE e.model = 'on-moved' AND e.user_id = (SELECT id FROM users WHERE login = :login)",
            login=_LOGIN,
        )
        assert moved == [(_TARGET_KEY,)]
    finally:
        _alembic("upgrade", "head")
        await _cleanup()
