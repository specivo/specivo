"""Migration 0032 round-trips: downgrade to 0031 and upgrade back to head.

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
            return {
                "revision": await conn.scalar(text("SELECT version_num FROM alembic_version")),
                "index": await conn.scalar(
                    text("SELECT count(*) FROM pg_indexes WHERE indexname = 'uq_roles_single_builtin'")
                ),
                "check": await conn.scalar(
                    text("SELECT count(*) FROM pg_constraint WHERE conname = 'ck_roles_builtin_not_assignable'")
                ),
                "triggers": await conn.scalar(
                    text(
                        "SELECT count(*) FROM pg_trigger WHERE tgname IN "
                        "('trg_protect_builtin_roles', 'trg_reject_builtin_role_membership')"
                    )
                ),
                "non_member_rows": (
                    await conn.execute(
                        text("SELECT name, assignable, permissions, issues_visibility FROM roles WHERE builtin = 1")
                    )
                ).all(),
            }
    finally:
        await engine.dispose()


async def test_downgrade_and_upgrade_round_trip() -> None:
    seeded = [("Non member", False, ["view_issues"], "default")]
    before = await _state()
    assert before["revision"] == "0032"
    assert [tuple(r) for r in before["non_member_rows"]] == seeded

    try:
        _alembic("downgrade", "0031")
        down = await _state()
        assert down["revision"] == "0031"
        assert (down["index"], down["check"], down["triggers"]) == (0, 0, 0)
        # The row is kept; nothing reads it at 0031.
        assert len(down["non_member_rows"]) == 1
    finally:
        _alembic("upgrade", "head")

    after = await _state()
    assert after["revision"] == "0032"
    assert (after["index"], after["check"], after["triggers"]) == (1, 1, 2)
    assert [tuple(r) for r in after["non_member_rows"]] == seeded
