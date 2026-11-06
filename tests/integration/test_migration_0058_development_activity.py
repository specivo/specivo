"""Migration 0058 adds the development activity tables and the ``manual`` link source, and round-trips.

Runs Alembic in a subprocess against the test database, so it changes schema
shared by every test and is marked ``serial``. The upgrade always ends at head,
even when an assertion fails half-way. Rows it inserts are committed for real
(Alembic runs on its own connection) and deleted at the end.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy.exc import IntegrityError

from tests.integration.test_migration_0056_repository_provider_history import _alembic, _execute

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration, pytest.mark.serial]

_PROJECT_KEY = "MIGDEV"
_TABLES = (
    "ci_runs",
    "deployments",
    "repository_releases",
    "issue_release_links",
    "pull_request_reviews",
    "development_detachments",
)
_INTEGRATION = "(SELECT r.id FROM repository_integrations r JOIN projects p ON p.id = r.project_id WHERE p.key = :key)"


async def _cleanup() -> None:
    await _execute("DELETE FROM projects WHERE key = :key", key=_PROJECT_KEY)


async def _tables() -> set[str]:
    rows = await _execute(
        "SELECT table_name FROM information_schema.tables WHERE table_name = ANY(:names)", names=list(_TABLES)
    )
    return {name for (name,) in rows}


async def _source_check() -> str:
    rows = await _execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ck_issue_commit_links_source'"
    )
    return rows[0][0]


async def test_upgrade_and_downgrade() -> None:
    assert os.environ.get("DATABASE_URL")
    await _cleanup()
    try:
        _alembic("downgrade", "0057")
        assert await _tables() == set()
        assert "manual" not in await _source_check()
        await _execute(
            "INSERT INTO projects (name, key, identifier, path, status, is_public, settings, issue_sequence) "
            "VALUES ('Migration development', :key, 'migdev', 'migdev', 1, false, '{}', 0)",
            key=_PROJECT_KEY,
        )
        await _execute(
            "INSERT INTO repository_integrations "
            "(public_id, project_id, provider, repo_full_name, repo_url, secret_encrypted, settings) "
            "SELECT gen_random_uuid(), id, 'github', 'example-org/widgets', "
            "'https://github.com/example-org/widgets', 'x', '{}' FROM projects WHERE key = :key",
            key=_PROJECT_KEY,
        )

        _alembic("upgrade", "0058")
        assert await _tables() == set(_TABLES)
        assert "manual" in await _source_check()
        await _execute(
            "INSERT INTO ci_runs (integration_id, provider, run_type, external_id, name, status, occurred_at) "
            f"VALUES ({_INTEGRATION}, 'github', 'workflow_run', 1, 'CI', 'success', now())",
            key=_PROJECT_KEY,
        )
        with pytest.raises(IntegrityError):
            await _execute(
                "INSERT INTO ci_runs (integration_id, provider, run_type, external_id, name, status, occurred_at) "
                f"VALUES ({_INTEGRATION}, 'github', 'workflow_run', 1, 'CI', 'success', now())",
                key=_PROJECT_KEY,
            )
        with pytest.raises(IntegrityError):
            await _execute(
                "INSERT INTO ci_runs (integration_id, provider, run_type, external_id, name, status, occurred_at) "
                f"VALUES ({_INTEGRATION}, 'github', 'workflow_run', 2, 'CI', 'exploded', now())",
                key=_PROJECT_KEY,
            )

        _alembic("downgrade", "0057")
        assert await _tables() == set()
        assert "manual" not in await _source_check()
        rows = await _execute(
            f"SELECT count(*) FROM repository_integrations WHERE id = {_INTEGRATION}", key=_PROJECT_KEY
        )
        assert rows == [(1,)]
    finally:
        _alembic("upgrade", "head")
        await _cleanup()
