"""Migration 0056 gives deliveries and pull requests their integration's provider, and round-trips.

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
_PROJECT_KEY = "MIGPRV"


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


_INTEGRATION = "(SELECT r.id FROM repository_integrations r JOIN projects p ON p.id = r.project_id WHERE p.key = :key)"


async def test_rows_take_the_integration_provider_and_numbers_are_per_provider() -> None:
    await _cleanup()
    try:
        _alembic("downgrade", "0055")
        await _execute(
            "INSERT INTO projects (name, key, identifier, path, status, is_public, settings, issue_sequence) "
            "VALUES ('Migration provider history', :key, 'migprv', 'migprv', 1, false, '{}', 0)",
            key=_PROJECT_KEY,
        )
        await _execute(
            "INSERT INTO repository_integrations "
            "(public_id, project_id, provider, repo_full_name, repo_url, secret_encrypted, settings) "
            "SELECT gen_random_uuid(), id, 'gitlab', 'example-group/widgets', "
            "'https://gitlab.example.org/example-group/widgets', 'x', '{}' FROM projects WHERE key = :key",
            key=_PROJECT_KEY,
        )
        await _execute(
            "INSERT INTO repository_deliveries (integration_id, delivery_id, event, status) "
            f"VALUES ({_INTEGRATION}, 'd-1', 'push', 'processed')",
            key=_PROJECT_KEY,
        )
        await _execute(
            "INSERT INTO pull_requests (integration_id, number, title, url, state) "
            f"VALUES ({_INTEGRATION}, 7, '', '', 'open')",
            key=_PROJECT_KEY,
        )

        _alembic("upgrade", "0056")
        for table in ("repository_deliveries", "pull_requests"):
            rows = await _execute(
                f"SELECT provider FROM {table} WHERE integration_id = {_INTEGRATION}",  # noqa: S608
                key=_PROJECT_KEY,
            )
            assert rows == [("gitlab",)], table

        # The same number from another provider is another request; from the same one it is not.
        await _execute(
            "INSERT INTO pull_requests (integration_id, provider, number, title, url, state) "
            f"VALUES ({_INTEGRATION}, 'github', 7, '', '', 'open')",
            key=_PROJECT_KEY,
        )
        with pytest.raises(IntegrityError):
            await _execute(
                "INSERT INTO pull_requests (integration_id, provider, number, title, url, state) "
                f"VALUES ({_INTEGRATION}, 'gitlab', 7, '', '', 'open')",
                key=_PROJECT_KEY,
            )

        # Down again: the other provider's request cannot fit the old key and goes.
        _alembic("downgrade", "0055")
        rows = await _execute(
            f"SELECT number FROM pull_requests WHERE integration_id = {_INTEGRATION}",  # noqa: S608
            key=_PROJECT_KEY,
        )
        assert rows == [(7,)]
    finally:
        _alembic("upgrade", "head")
        await _cleanup()
