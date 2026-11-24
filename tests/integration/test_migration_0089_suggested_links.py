"""Migration 0089 adds document vectors, suggested pairs and the typed link kind, and round-trips.

After the upgrade ``entity_vectors`` and ``entity_suggestions`` exist with
their unique indexes (the pair once per model, ``NULLS NOT DISTINCT``) and
checks, every foreign key cascades as described, and ``entity_links`` has
``link_type`` with its check. The downgrade removes all of it.

Runs Alembic in a subprocess against the test database, so it changes schema
shared by every test and is marked ``serial``. The upgrade always ends at head,
even when an assertion fails half-way.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration, pytest.mark.serial]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION = _REPO_ROOT / "alembic" / "versions" / "0089_suggested_links.py"


def _revisions() -> tuple[str, str]:
    """This migration's revision and the one it follows, read from the file (the chain may be rebased)."""
    source = _MIGRATION.read_text(encoding="utf-8")
    revision = re.search(r'^revision = "([^"]+)"', source, re.M)
    down = re.search(r'^down_revision = "([^"]+)"', source, re.M)
    assert revision and down
    return revision.group(1), down.group(1)


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


async def _rows(sql: str) -> list:
    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        async with engine.begin() as conn:
            return list((await conn.execute(text(sql))).all())
    finally:
        await engine.dispose()


async def _schema() -> dict[str, set[str]]:
    tables = {
        r[0]
        for r in await _rows(
            "SELECT tablename FROM pg_tables WHERE tablename IN ('entity_vectors', 'entity_suggestions')"
        )
    }
    columns = {
        r[0]
        for r in await _rows(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'entity_links' AND column_name = 'link_type'"
        )
    }
    constraints = {
        r[0]
        for r in await _rows(
            "SELECT conname FROM pg_constraint WHERE conname LIKE 'ck_entity_suggestions_%' "
            "OR conname = 'ck_entity_links_link_type'"
        )
    }
    indexes = {r[0] for r in await _rows("SELECT indexname FROM pg_indexes WHERE indexname LIKE 'uq_entity_%'")}
    return {"tables": tables, "columns": columns, "constraints": constraints, "indexes": indexes}


async def test_suggested_links_schema_is_added_and_the_downgrade_round_trips() -> None:
    revision, down = _revisions()
    try:
        _alembic("downgrade", down)
        before = await _schema()
        assert before["tables"] == set() and before["columns"] == set() and before["constraints"] == set()
        assert "uq_entity_suggestions_pair" not in before["indexes"]

        _alembic("upgrade", revision)
        after = await _schema()
        assert after["tables"] == {"entity_vectors", "entity_suggestions"}
        assert after["columns"] == {"link_type"}
        assert after["constraints"] == {
            "ck_entity_links_link_type",
            "ck_entity_suggestions_one_source",
            "ck_entity_suggestions_one_target",
            "ck_entity_suggestions_status",
            "ck_entity_suggestions_score",
        }
        assert {"uq_entity_suggestions_pair", "uq_entity_vectors_source_model"} <= after["indexes"]
        [(nulls_not_distinct,)] = await _rows(
            "SELECT indnullsnotdistinct FROM pg_index WHERE indexrelid = 'uq_entity_suggestions_pair'::regclass"
        )
        assert nulls_not_distinct is True
        deletes = dict(
            await _rows(
                "SELECT a.attname, c.confdeltype::text FROM pg_constraint c "
                "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey) "
                "WHERE c.conrelid = 'entity_suggestions'::regclass AND c.contype = 'f'"
            )
        )
        assert deletes == {
            "project_id": "c",
            "model_id": "c",
            "source_issue_id": "c",
            "source_wiki_page_id": "c",
            "target_issue_id": "c",
            "target_wiki_page_id": "c",
            "decided_by_id": "n",
        }

        _alembic("downgrade", down)
        assert (await _schema())["tables"] == set()
    finally:
        _alembic("upgrade", "head")
