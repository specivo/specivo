"""Migration 0065 marks fixing links already acted on, leaves waiting ones open, and round-trips.

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
_PROJECT_KEY = "MIGFIX"
_NAME = "Migration 0065"


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
    await _execute("DELETE FROM projects WHERE key = :key", key=_PROJECT_KEY)
    for table in ("trackers", "issue_statuses", "issue_priorities"):
        await _execute(f"DELETE FROM {table} WHERE name = :name", name=_NAME)  # noqa: S608
    await _execute("DELETE FROM users WHERE login = 'migfix_author'")


_INTEGRATION = "(SELECT r.id FROM repository_integrations r JOIN projects p ON p.id = r.project_id WHERE p.key = :key)"


async def _seed() -> None:
    await _execute(
        "INSERT INTO projects (name, key, identifier, path, status, is_public, settings, issue_sequence) "
        "VALUES ('Migration fix ran at', :key, 'migfix', 'migfix', 1, false, '{}', 0)",
        key=_PROJECT_KEY,
    )
    await _execute("INSERT INTO users (login, email, display_name) VALUES ('migfix_author', 'migfix@example.com', 'M')")
    for table in ("trackers", "issue_statuses", "issue_priorities"):
        await _execute(f"INSERT INTO {table} (name) VALUES (:name)", name=_NAME)  # noqa: S608
    await _execute(
        "INSERT INTO issues (project_id, project_key, sequence_number, tracker_id, status_id, priority_id, "
        "author_id, subject, lock_version) "
        "SELECT p.id, p.key, 1, t.id, s.id, pr.id, u.id, 'Migrated', 0 "
        "FROM projects p, trackers t, issue_statuses s, issue_priorities pr, users u "
        "WHERE p.key = :key AND t.name = :name AND s.name = :name AND pr.name = :name AND u.login = 'migfix_author'",
        key=_PROJECT_KEY,
        name=_NAME,
    )
    await _execute(
        "INSERT INTO repository_integrations "
        "(public_id, project_id, provider, repo_full_name, repo_url, secret_encrypted, settings, default_branch) "
        "SELECT gen_random_uuid(), id, 'github', 'example-org/widgets', "
        "'https://github.com/example-org/widgets', 'x', '{}', 'main' FROM projects WHERE key = :key",
        key=_PROJECT_KEY,
    )
    for sha, branch in (("a" * 40, "main"), ("b" * 40, "feat/login"), ("c" * 40, "main")):
        await _execute(
            f"INSERT INTO repository_commits (integration_id, sha, branch, message, url, author_name, author_email) "
            f"VALUES ({_INTEGRATION}, :sha, :branch, '', '', '', '')",  # noqa: S608
            key=_PROJECT_KEY,
            sha=sha,
            branch=branch,
        )
    # a: Fixes on main (ran), b: Fixes on a feature branch (waits), c: Refs on main (no command).
    for sha, keyword in (("a" * 40, "fixes"), ("b" * 40, "fixes"), ("c" * 40, "refs")):
        await _execute(
            "INSERT INTO issue_commit_links (commit_id, issue_id, source, keyword) "
            "SELECT c.id, i.id, 'message', :keyword FROM repository_commits c, issues i "
            f"WHERE c.sha = :sha AND c.integration_id = {_INTEGRATION} AND i.project_key = :key",  # noqa: S608
            key=_PROJECT_KEY,
            sha=sha,
            keyword=keyword,
        )


async def test_backfill_marks_only_fixing_links_first_seen_on_the_default_branch() -> None:
    await _cleanup()
    try:
        _alembic("downgrade", "0064")
        await _seed()

        _alembic("upgrade", "0065")
        rows = await _execute(
            "SELECT c.sha, l.fix_ran_at IS NOT NULL, l.fix_ran_at = l.created_at FROM issue_commit_links l "
            f"JOIN repository_commits c ON c.id = l.commit_id WHERE c.integration_id = {_INTEGRATION} "  # noqa: S608
            "ORDER BY c.sha",
            key=_PROJECT_KEY,
        )
        assert rows == [("a" * 40, True, True), ("b" * 40, False, None), ("c" * 40, False, None)]

        _alembic("downgrade", "0064")
        columns = await _execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'issue_commit_links' AND column_name = 'fix_ran_at'"
        )
        assert columns == []
    finally:
        _alembic("upgrade", "head")
        await _cleanup()
