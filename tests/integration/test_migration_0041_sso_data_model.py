"""Migration 0041 adds the SSO tables and drops the unused OAuth placeholder columns.

Runs Alembic in a subprocess against the test database, so it changes the
schema shared by every test and is marked ``serial``. The database always ends
at head, even when an assertion fails half-way.
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
_TABLES = {"sso_providers", "user_identities", "sso_group_mappings"}


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


async def _scalars(sql: str, params: dict | None = None) -> list:
    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        async with engine.connect() as conn:
            return list((await conn.execute(text(sql), params or {})).scalars())
    finally:
        await engine.dispose()


async def _revision() -> str:
    return (await _scalars("SELECT version_num FROM alembic_version"))[0]


async def _tables() -> set[str]:
    return set(
        await _scalars(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_name = ANY(:t)",
            {"t": sorted(_TABLES)},
        )
    )


async def _columns(table: str) -> set[str]:
    return set(await _scalars("SELECT column_name FROM information_schema.columns WHERE table_name = :t", {"t": table}))


async def _indexes(table: str) -> set[str]:
    return set(await _scalars("SELECT indexname FROM pg_indexes WHERE tablename = :t", {"t": table}))


async def _function_exists(name: str) -> bool:
    return bool(await _scalars("SELECT 1 FROM pg_proc WHERE proname = :n", {"n": name}))


async def _assert_upgraded() -> None:
    assert await _revision() >= "0041"
    assert await _tables() == _TABLES
    users = await _columns("users")
    assert "github_id" not in users and "google_id" not in users
    assert not {"ix_users_github_id", "ix_users_google_id"} & await _indexes("users")
    assert "managed_by_provider_id" in await _columns("user_group_members")
    assert "ix_user_group_members_managed_by_provider_id" in await _indexes("user_group_members")
    assert await _function_exists("reject_sso_identity_for_inert_user")


async def test_upgrade_downgrade_upgrade_round_trip() -> None:
    assert await _revision() >= "0041"
    try:
        _alembic("downgrade", "0041-1")
        assert await _revision() == "0040"
        assert await _tables() == set()
        users = await _columns("users")
        assert {"github_id", "google_id"} <= users
        assert {"ix_users_github_id", "ix_users_google_id"} <= await _indexes("users")
        assert "managed_by_provider_id" not in await _columns("user_group_members")
        assert not await _function_exists("reject_sso_identity_for_inert_user")

        _alembic("upgrade", "head")
        await _assert_upgraded()
    finally:
        _alembic("upgrade", "head")
