"""Fixtures for importer tests that run against a real Redmine.

These read the Redmine fixture started by ``make redmine-fixture-up`` and write
into the ordinary rollback-isolated test session, so a full import can be
exercised without leaving anything behind.

The pipeline normally opens a session per phase and commits each one. Here it
is handed the test's session instead, which keeps every phase inside the
transaction the fixture rolls back. That costs one property — a failed phase no
longer leaves earlier phases committed — and nothing else: phase ordering,
identifier mapping and the skip-what-exists behaviour are unaffected.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text

from specivo.importers.core.pipeline import ImportOptions, ImportPipeline
from specivo.importers.core.progress import NullProgressReporter
from specivo.importers.load.registry import register_all
from specivo.importers.redmine import RedmineSourceAdapter

# Where the fixture publishes each profile. Overridable so the same tests can
# run against a Redmine somewhere else.
PROFILES = {
    "pg": {
        "url": os.environ.get("REDMINE_FIXTURE_PG_URL", "postgresql://redmine:redmine@127.0.0.1:5444/redmine"),
        "files": os.environ.get("REDMINE_FIXTURE_PG_FILES", ".redmine-fixture/files-pg"),
    },
    "mysql": {
        "url": os.environ.get("REDMINE_FIXTURE_MYSQL_URL", "mysql://redmine:redmine@127.0.0.1:3307/redmine"),
        "files": os.environ.get("REDMINE_FIXTURE_MYSQL_FILES", ".redmine-fixture/files-mysql"),
    },
}

# The seed creates this account first, so its presence means the fixture has
# content rather than just a schema.
SEED_MARKER_LOGIN = "fixture_admin"


@pytest.fixture(params=["pg", "mysql"])
def redmine_profile(request) -> dict[str, str]:
    """Return the connection details for one fixture profile.

    Each profile is skipped independently: running only the PostgreSQL one is
    the normal case, and the MySQL one is there to prove the dialect layer.
    """
    return PROFILES[request.param]


@pytest_asyncio.fixture
async def redmine_adapter(redmine_profile):
    """A live adapter, skipping when that profile is not up or not seeded."""
    adapter = RedmineSourceAdapter(
        source_db_url=redmine_profile["url"],
        source_files_dir=Path(redmine_profile["files"]).resolve(),
        source_instance="pytest-fixture",
    )
    try:
        await adapter.connect()
    except Exception as exc:  # noqa: BLE001 - any connection failure means "not running"
        pytest.skip(f"Redmine fixture not reachable: {exc}")

    async with adapter.engine.connect() as conn:
        seeded = (
            await conn.execute(text("SELECT count(*) FROM users WHERE login = :login"), {"login": SEED_MARKER_LOGIN})
        ).scalar_one()
    if not seeded:
        await adapter.close()
        pytest.skip("Redmine fixture is running but not seeded (make redmine-fixture-seed)")

    yield adapter
    await adapter.close()


class _SharedSession:
    """Hands the pipeline the test's session and refuses to close it."""

    def __init__(self, session) -> None:
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc_info) -> None:
        return None


@pytest.fixture
def build_import(db_session, redmine_adapter, tmp_path, monkeypatch):
    """Return a factory building a wired pipeline against the live fixture.

    Attachment storage is redirected to a temporary directory, so a run that
    copies files leaves them there rather than in the instance's own storage.
    """
    import specivo.services.attachment_service as attachment_service

    storage = tmp_path / "attachments"
    storage.mkdir()
    monkeypatch.setattr(attachment_service, "_upload_dir", storage)

    def _build(**option_kwargs) -> ImportPipeline:
        options = ImportOptions(source_instance=redmine_adapter.source_instance, **option_kwargs)
        return register_all(
            ImportPipeline(
                adapter=redmine_adapter,
                session_factory=lambda: _SharedSession(db_session),
                options=options,
                reporter=NullProgressReporter(),
            )
        )

    _build.storage = storage  # type: ignore[attr-defined]
    return _build
