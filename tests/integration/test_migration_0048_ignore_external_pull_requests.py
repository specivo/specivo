"""Migration 0048 turns ``ignore_external_pull_requests`` on for existing integrations and round-trips.

Runs Alembic in a subprocess against the test database, so it changes schema
shared by every test and is marked ``serial``. The upgrade always ends at head,
even when an assertion fails half-way. Rows it inserts are committed for real
(Alembic runs on its own connection) and deleted at the end.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration, pytest.mark.serial]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROJECT_KEY = "MIGXPR"
_KEY = "ignore_external_pull_requests"


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


async def _settings() -> dict[str, dict]:
    rows = await _execute(
        "SELECT r.repo_full_name, r.settings FROM repository_integrations r "
        "JOIN projects p ON p.id = r.project_id WHERE p.key = :key",
        key=_PROJECT_KEY,
    )
    return {name: settings for name, settings in rows}


async def test_existing_integrations_turn_it_on_and_downgrade_removes_it() -> None:
    await _cleanup()
    try:
        _alembic("downgrade", "0047")
        await _execute(
            "INSERT INTO projects (name, key, identifier, path, status, is_public, settings, issue_sequence) "
            "VALUES ('Migration external PRs', :key, 'migxpr', 'migxpr', 1, false, '{}', 0)",
            key=_PROJECT_KEY,
        )
        for name, settings in (
            ("example-org/plain", {}),
            ("example-org/commands", {"commands_enabled": True, "time_logging_enabled": False}),
        ):
            await _execute(
                "INSERT INTO repository_integrations "
                "(public_id, project_id, provider, repo_full_name, repo_url, secret_encrypted, settings) "
                "SELECT gen_random_uuid(), id, 'github', :name, :url, 'x', "
                "CAST(:settings AS jsonb) FROM projects WHERE key = :key",
                name=name,
                url=f"https://forge.example.com/{name}",
                settings=json.dumps(settings),
                key=_PROJECT_KEY,
            )

        _alembic("upgrade", "0048")
        assert await _settings() == {
            "example-org/plain": {_KEY: True},
            "example-org/commands": {"commands_enabled": True, "time_logging_enabled": False, _KEY: True},
        }

        await _execute(
            "UPDATE repository_integrations SET settings = jsonb_set(settings, :path, 'false') "
            "WHERE repo_full_name = 'example-org/plain'",
            path=[_KEY],
        )
        _alembic("downgrade", "0047")
        assert await _settings() == {
            "example-org/plain": {},
            "example-org/commands": {"commands_enabled": True, "time_logging_enabled": False},
        }
    finally:
        _alembic("upgrade", "head")
        await _cleanup()
