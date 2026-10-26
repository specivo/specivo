"""Migration 0040 round-trips: ``specivo_fts_text`` and the triggers that call it come and go.

Runs Alembic in a subprocess against the test database, so it changes schema
shared by every test and is marked ``serial``. The upgrade always ends at head,
even when an assertion fails half-way.
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

_TRIGGER_FUNCTIONS = (
    "issues_search_vector_update",
    "wiki_contents_search_vector_update",
    "search_chunks_search_vector_update",
)


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


async def _state() -> dict[str, object]:
    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        async with engine.connect() as conn:
            function_exists = await conn.scalar(text("SELECT count(*) FROM pg_proc WHERE proname = 'specivo_fts_text'"))
            calling = await conn.scalar(
                text("SELECT count(*) FROM pg_proc WHERE proname = ANY(:names) AND prosrc LIKE '%specivo_fts_text%'"),
                {"names": list(_TRIGGER_FUNCTIONS)},
            )
            still_identifiers = await conn.scalar(
                text(
                    "SELECT count(*) FROM pg_proc WHERE proname = ANY(:names) "
                    "AND prosrc LIKE '%specivo_fts_identifiers%'"
                ),
                {"names": list(_TRIGGER_FUNCTIONS)},
            )
            sanitised = None
            if function_exists:
                sanitised = await conn.scalar(text("SELECT specivo_fts_text('<!-- a --> b <!-- c')"))
            return {
                "function": function_exists,
                "calling_triggers": calling,
                "identifier_triggers": still_identifiers,
                "sanitised": sanitised,
            }
    finally:
        await engine.dispose()


async def test_downgrade_and_upgrade_round_trip() -> None:
    before = await _state()
    assert before == {
        "function": 1,
        "calling_triggers": 3,
        "identifier_triggers": 3,
        "sanitised": "<!-- a --> b  !-- c",
    }

    try:
        _alembic("downgrade", "0040-1")
        down = await _state()
        # Back to the 0034 triggers: identifier lexemes kept, no sanitiser.
        assert down == {"function": 0, "calling_triggers": 0, "identifier_triggers": 3, "sanitised": None}
    finally:
        _alembic("upgrade", "head")

    assert await _state() == before
