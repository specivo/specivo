"""Migration 0090 adds the project AI provider and the concept tables, and round-trips.

After the upgrade ``project_ai_providers``, ``entity_concepts``,
``concept_mentions`` and ``concept_extraction_sources`` exist with their unique
indexes (mentions and sources once per source, ``NULLS NOT DISTINCT``) and
checks, and every foreign key cascades as described. The downgrade removes
them all.

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
_MIGRATION = _REPO_ROOT / "alembic" / "versions" / "0090_concept_extraction.py"
_TABLES = {"project_ai_providers", "entity_concepts", "concept_mentions", "concept_extraction_sources"}


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


async def _tables() -> set[str]:
    names = ", ".join(f"'{t}'" for t in sorted(_TABLES))
    return {r[0] for r in await _rows(f"SELECT tablename FROM pg_tables WHERE tablename IN ({names})")}


async def _deletes(table: str) -> dict[str, str]:
    return dict(
        await _rows(
            "SELECT a.attname, c.confdeltype::text FROM pg_constraint c "
            "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey) "
            f"WHERE c.conrelid = '{table}'::regclass AND c.contype = 'f'"
        )
    )


async def test_concept_schema_is_added_and_the_downgrade_round_trips() -> None:
    revision, down = _revisions()
    try:
        _alembic("downgrade", down)
        assert await _tables() == set()

        _alembic("upgrade", revision)
        assert await _tables() == _TABLES
        constraints = {
            r[0]
            for r in await _rows(
                "SELECT conname FROM pg_constraint WHERE conname LIKE 'ck_entity_concepts_%' "
                "OR conname LIKE 'ck_concept_%'"
            )
        }
        assert constraints == {
            "ck_entity_concepts_kind",
            "ck_concept_mentions_one_source",
            "ck_concept_mentions_confidence",
            "ck_concept_mentions_span",
            "ck_concept_extraction_sources_one_source",
            "ck_concept_extraction_sources_status",
        }
        unique = {
            r[0]: r[1]
            for r in await _rows(
                "SELECT c.relname, i.indnullsnotdistinct FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE i.indisunique AND c.relname IN ('uq_entity_concepts_project_normalized', "
                "'uq_concept_mentions_concept_source', 'uq_concept_extraction_sources_source', "
                "'ix_project_ai_providers_project_id')"
            )
        }
        assert unique == {
            "uq_entity_concepts_project_normalized": False,
            "uq_concept_mentions_concept_source": True,
            "uq_concept_extraction_sources_source": True,
            "ix_project_ai_providers_project_id": False,
        }
        assert await _deletes("project_ai_providers") == {"project_id": "c", "updated_by_id": "n"}
        assert await _deletes("entity_concepts") == {"project_id": "c"}
        assert await _deletes("concept_mentions") == {
            "concept_id": "c",
            "source_issue_id": "c",
            "source_wiki_page_id": "c",
        }
        assert await _deletes("concept_extraction_sources") == {
            "project_id": "c",
            "source_issue_id": "c",
            "source_wiki_page_id": "c",
        }

        _alembic("downgrade", down)
        assert await _tables() == set()
    finally:
        _alembic("upgrade", "head")
