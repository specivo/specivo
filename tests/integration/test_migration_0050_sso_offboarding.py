"""Migration 0050: session re-check at the IdP and back-channel actions; round-trips.

Existing providers get ``verify_session_at_idp`` on (safe: a session without a
stored IdP refresh token is renewed as before), ``idp_check_failure=allow`` and
``backchannel_action=revoke_session`` (today's behaviour). Existing session
rows carry no IdP refresh token.

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
_SLUG = "mig0050-idp"
_PROVIDER_COLUMNS = (
    "verify_session_at_idp",
    "idp_check_failure",
    "backchannel_action",
    "session_recheck_status",
    "session_recheck_status_at",
)
_SESSION_COLUMNS = ("idp_refresh_token_encrypted", "idp_checked_at")


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


async def test_existing_providers_get_the_defaults_and_round_trip() -> None:
    assert set(_PROVIDER_COLUMNS) <= await _columns("sso_providers")
    await _cleanup()

    try:
        _alembic("downgrade", "0049")
        assert not set(_PROVIDER_COLUMNS) & await _columns("sso_providers")
        assert not set(_SESSION_COLUMNS) & await _columns("refresh_tokens")
        await _execute(
            "INSERT INTO sso_providers (slug, display_name, issuer, client_id) "
            "VALUES (:slug, 'Migration IdP', 'https://idp.example.com', 'client')",
            slug=_SLUG,
        )

        _alembic("upgrade", "0050")
        [row] = await _execute(
            "SELECT verify_session_at_idp, idp_check_failure, backchannel_action, session_recheck_status "
            "FROM sso_providers WHERE slug = :slug",
            slug=_SLUG,
        )
        assert tuple(row) == (True, "allow", "revoke_session", None)
        assert set(_SESSION_COLUMNS) <= await _columns("refresh_tokens")

        # The check constraints hold.
        for column, value in (("idp_check_failure", "maybe"), ("backchannel_action", "delete_user")):
            with pytest.raises(Exception, match="ck_sso_providers_"):
                await _execute(
                    f"UPDATE sso_providers SET {column} = :value WHERE slug = :slug", value=value, slug=_SLUG
                )

        _alembic("downgrade", "0049")
        assert not set(_PROVIDER_COLUMNS) & await _columns("sso_providers")
    finally:
        _alembic("upgrade", "head")
        await _cleanup()

    assert set(_SESSION_COLUMNS) <= await _columns("refresh_tokens")
