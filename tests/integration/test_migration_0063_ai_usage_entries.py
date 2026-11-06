"""Migration 0063: ai_usage_entries, roles.ai_usage_visibility and the seeded role grants; and its downgrade.

Runs Alembic in a subprocess against the test database, so it changes schema
shared by every test and is marked ``serial``. The upgrade always ends at head,
even when an assertion fails half-way.
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

_SEEDED = {
    "Manager": (["*"], "all"),
    "Developer": (["view_issues", "log_time"], "own"),
    "Reporter": (["view_issues"], "own"),
    "Agent": (["view_issues", "log_time"], "own"),
}


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


async def _table_exists(name: str) -> bool:
    rows = await _execute("SELECT to_regclass(:name) IS NOT NULL", name=f"public.{name}")
    return bool(rows[0][0])


async def _column_exists(table: str, column: str) -> bool:
    rows = await _execute(
        "SELECT count(*) FROM information_schema.columns WHERE table_name = :t AND column_name = :c", t=table, c=column
    )
    return bool(rows[0][0])


async def test_head_has_the_table_its_constraints_and_indexes() -> None:
    assert await _table_exists("ai_usage_entries")
    assert await _column_exists("roles", "ai_usage_visibility")
    constraints = {
        r[0] for r in await _execute("SELECT conname FROM pg_constraint WHERE conrelid = 'ai_usage_entries'::regclass")
    }
    assert {
        "ck_ai_usage_entries_key_required",
        "ck_ai_usage_entries_issue_needs_project",
        "ck_ai_usage_entries_counts_non_negative",
        "ck_ai_usage_entries_period",
        "ck_ai_usage_entries_marginal_money_pair",
        "ai_usage_entries_price_id_fkey",
        "ai_usage_entries_user_id_fkey",
    } <= constraints
    delete_rules = dict(
        await _execute(
            "SELECT conname, confdeltype::text FROM pg_constraint WHERE conrelid = 'ai_usage_entries'::regclass "
            "AND contype = 'f'"
        )
    )
    assert delete_rules["ai_usage_entries_price_id_fkey"] == "r"  # RESTRICT
    assert delete_rules["ai_usage_entries_issue_id_fkey"] == "n"  # SET NULL
    assert delete_rules["ai_usage_entries_user_id_fkey"] == "r"
    indexes = {r[0] for r in await _execute("SELECT indexname FROM pg_indexes WHERE tablename = 'ai_usage_entries'")}
    assert {
        "uq_ai_usage_entries_user_key",
        "ix_ai_usage_entries_project_date",
        "ix_ai_usage_entries_user_date",
        "ix_ai_usage_entries_issue_id",
        "ix_ai_usage_entries_reporter_date",
        "ix_ai_usage_entries_model_priced_at",
    } <= indexes


async def test_seeded_roles_get_their_grants_and_the_downgrade_removes_them() -> None:
    names = list(_SEEDED)
    try:
        _alembic("downgrade", "0062")
        assert not await _table_exists("ai_usage_entries")
        assert not await _column_exists("roles", "ai_usage_visibility")
        # Recreate the seeded roles as they were before this migration (by name, builtin = 0).
        await _execute("DELETE FROM member_roles WHERE role_id IN (SELECT id FROM roles WHERE name = ANY(:n))", n=names)
        await _execute(
            "DELETE FROM workflow_transitions WHERE role_id IN (SELECT id FROM roles WHERE name = ANY(:n))", n=names
        )
        await _execute("DELETE FROM roles WHERE name = ANY(:n)", n=names)
        for position, (name, (permissions, _vis)) in enumerate(_SEEDED.items(), start=1):
            await _execute(
                "INSERT INTO roles (name, position, assignable, builtin, permissions, issues_visibility, settings) "
                "VALUES (:name, :pos, true, 0, CAST(:perms AS jsonb), 'default', '{}'::jsonb)",
                name=name,
                pos=position + 100,
                perms=json.dumps(permissions),
            )
    finally:
        _alembic("upgrade", "head")
    rows = {
        name: (list(perms), vis)
        for name, perms, vis in await _execute(
            "SELECT name, permissions, ai_usage_visibility FROM roles WHERE name = ANY(:n)", n=names
        )
    }
    assert rows["Manager"] == (["*"], "all")
    assert rows["Developer"] == (["view_issues", "log_time", "view_ai_usage", "log_ai_usage"], "own")
    assert rows["Reporter"] == (["view_issues", "view_ai_usage"], "own")
    assert rows["Agent"] == (["view_issues", "log_time", "view_ai_usage", "log_ai_usage"], "own")

    try:
        _alembic("downgrade", "0062")
        after_down = {
            name: list(perms)
            for name, perms in await _execute("SELECT name, permissions FROM roles WHERE name = ANY(:n)", n=names)
        }
        assert after_down["Developer"] == ["view_issues", "log_time"]
        assert after_down["Reporter"] == ["view_issues"]
    finally:
        _alembic("upgrade", "head")
        await _execute("DELETE FROM roles WHERE name = ANY(:n) AND position > 100", n=names)
