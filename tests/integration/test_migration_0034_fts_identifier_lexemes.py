"""Migration 0034 round-trips: the identifier lexeme function and triggers come and go.

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
            function_exists = await conn.scalar(
                text("SELECT count(*) FROM pg_proc WHERE proname = 'specivo_fts_identifiers'")
            )
            calling = await conn.scalar(
                text(
                    "SELECT count(*) FROM pg_proc WHERE proname = ANY(:names) "
                    "AND prosrc LIKE '%specivo_fts_identifiers%'"
                ),
                {"names": list(_TRIGGER_FUNCTIONS)},
            )
            lexemes = None
            if function_exists:
                lexemes = await conn.scalar(
                    text("SELECT CAST(specivo_fts_identifiers('Fixes [DO-8795] and acme-42, not e-mail') AS text)")
                )
            return {"function": function_exists, "calling_triggers": calling, "lexemes": lexemes}
    finally:
        await engine.dispose()


async def test_downgrade_and_upgrade_round_trip() -> None:
    before = await _state()
    assert before == {"function": 1, "calling_triggers": 3, "lexemes": "'acme-42':1 'do-8795':1"}

    try:
        _alembic("downgrade", "-1")
        down = await _state()
        assert down == {"function": 0, "calling_triggers": 0, "lexemes": None}
    finally:
        _alembic("upgrade", "head")

    assert await _state() == before
