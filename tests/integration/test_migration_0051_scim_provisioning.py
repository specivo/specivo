"""Migration 0051: SCIM provisioning columns and table; round-trips.

Existing providers get SCIM off and no token, users a ``deactivation_reason``; the check constraint keeps SCIM
off without a token. A downgrade drops ``scim_resources`` and turns identities
linked through SCIM into ``admin`` links, which the older constraint allows.

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
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration, pytest.mark.serial]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SLUG = "mig0051-idp"
_PROVIDER_COLUMNS = (
    "scim_enabled",
    "scim_token_hash",
    "scim_token_prefix",
    "scim_token_created_at",
    "scim_last_used_at",
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


async def _execute(sql: str, **params) -> list:
    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(sql), params)
            return list(result.all()) if result.returns_rows else []
    finally:
        await engine.dispose()


async def _columns(table: str) -> set[str]:
    rows = await _execute("SELECT column_name FROM information_schema.columns WHERE table_name = :table", table=table)
    return {row[0] for row in rows}


async def _cleanup() -> None:
    await _execute("DELETE FROM sso_providers WHERE slug = :slug", slug=_SLUG)


async def test_scim_schema_round_trips() -> None:
    assert set(_PROVIDER_COLUMNS) <= await _columns("sso_providers")
    assert await _columns("scim_resources")
    assert "deactivation_reason" in await _columns("users")
    await _cleanup()

    try:
        _alembic("downgrade", "0050")
        assert not set(_PROVIDER_COLUMNS) & await _columns("sso_providers")
        assert not await _columns("scim_resources")
        assert "deactivation_reason" not in await _columns("users")
        await _execute(
            "INSERT INTO sso_providers (slug, display_name, issuer, client_id) "
            "VALUES (:slug, 'Migration IdP', 'https://idp.example.com', 'client')",
            slug=_SLUG,
        )

        _alembic("upgrade", "0051")
        assert "deactivation_reason" in await _columns("users")
        [row] = await _execute("SELECT scim_enabled, scim_token_hash FROM sso_providers WHERE slug = :slug", slug=_SLUG)
        assert tuple(row) == (False, None)
        with pytest.raises(Exception, match="ck_sso_providers_scim_needs_token"):
            await _execute("UPDATE sso_providers SET scim_enabled = true WHERE slug = :slug", slug=_SLUG)
        with pytest.raises(Exception, match="ck_scim_resources_one_target"):
            await _execute(
                "INSERT INTO scim_resources (provider_id, resource_type, scim_id) "
                "SELECT id, 'User', 'mig0051-x' FROM sso_providers WHERE slug = :slug",
                slug=_SLUG,
            )

        _alembic("downgrade", "0050")
        assert not set(_PROVIDER_COLUMNS) & await _columns("sso_providers")
    finally:
        _alembic("upgrade", "head")
        await _cleanup()

    assert await _columns("scim_resources")
