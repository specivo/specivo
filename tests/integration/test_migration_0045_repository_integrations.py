"""Migration 0045: the widened commit pattern, the ``$git`` account and its clashes, the old hook settings.

Runs Alembic in a subprocess against the test database, so it changes schema
shared by every test and is marked ``serial``. The upgrade always ends at head,
even when an assertion fails half-way. Rows it inserts are committed for real
(Alembic runs on its own connection) and deleted at the end.
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
_PROJECT_KEY = "MIGRI"
_PRESET = "mig0045-dev"
_OLD = "^[a-f0-9]{7,40}$"
_NEW = "^[a-f0-9]{7,64}$"
_OTHER = "^[a-f0-9]{40}$"
_HOOK_SETTINGS = ("github_webhook_secret", "gitlab_webhook_token")
_TABLES = ("repository_integrations", "repository_deliveries", "repository_commits", "issue_commit_links")
_LOGINS = ("mig45_clash", "$mig45_import", "$mig45_admin")


def _run_alembic(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=_REPO_ROOT,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=False,
    )


def _alembic(*args: str) -> None:
    result = _run_alembic(*args)
    assert result.returncode == 0, result.stdout + result.stderr


async def _execute(sql: str, **params) -> list:
    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(sql), params)
            return list(result.all()) if result.returns_rows else []
    finally:
        await engine.dispose()


async def _cleanup() -> None:
    await _execute("DELETE FROM projects WHERE key = :key", key=_PROJECT_KEY)
    await _execute("DELETE FROM metadata_presets WHERE slug LIKE :slug", slug=f"{_PRESET}%")
    await _execute("DELETE FROM users WHERE login = ANY(:logins)", logins=list(_LOGINS))


async def _tables() -> set[str]:
    rows = await _execute(
        "SELECT table_name FROM information_schema.tables WHERE table_name = ANY(:names)", names=list(_TABLES)
    )
    return {row[0] for row in rows}


async def _has_inert_check() -> bool:
    rows = await _execute("SELECT 1 FROM pg_constraint WHERE conname = 'ck_users_system_principal_inert'")
    return bool(rows)


def _schema(pattern: str) -> str:
    commits = {"type": "array", "items": {"type": "string", "pattern": pattern}}
    return json.dumps({"type": "object", "properties": {"commits": commits}})


async def _patterns() -> dict[str, str]:
    path = "{properties,commits,items,pattern}"
    presets = await _execute(
        f"SELECT slug, schema_definition #>> '{path}' FROM metadata_presets WHERE slug LIKE :slug",
        slug=f"{_PRESET}%",
    )
    schemas = await _execute(
        f"SELECT s.name, s.schema_definition #>> '{path}' FROM metadata_schemas s "
        "JOIN projects p ON p.id = s.project_id WHERE p.key = :key",
        key=_PROJECT_KEY,
    )
    return {name: pattern for name, pattern in [*presets, *schemas]}


async def _seed_before_upgrade() -> None:
    """Rows as they were before 0045: the old pattern in a preset and a schema, the old hook secrets."""
    for slug, pattern in ((_PRESET, _OLD), (f"{_PRESET}-other", _OTHER)):
        await _execute(
            "INSERT INTO metadata_presets (slug, name, schema_definition, is_builtin) "
            "VALUES (:slug, :slug, CAST(:schema AS jsonb), false)",
            slug=slug,
            schema=_schema(pattern),
        )
    await _execute(
        "INSERT INTO projects (name, key, identifier, path, status, is_public, settings, issue_sequence) "
        "VALUES ('Migration repositories', :key, 'migri', 'migri', 1, false, '{}', 0)",
        key=_PROJECT_KEY,
    )
    await _execute(
        "INSERT INTO metadata_schemas (project_id, name, schema_definition, preset_slug) "
        "SELECT id, 'mig0045-schema', CAST(:schema AS jsonb), :slug FROM projects WHERE key = :key",
        schema=_schema(_OLD),
        slug=_PRESET,
        key=_PROJECT_KEY,
    )
    for key in _HOOK_SETTINGS:
        await _execute(
            "INSERT INTO settings (key, value) VALUES (:key, 'old-secret') ON CONFLICT (key) DO NOTHING", key=key
        )


async def _insert_user(login: str, email: str, *, password_hash: str | None = None, is_admin: bool = False) -> None:
    await _execute(
        "INSERT INTO users (login, email, display_name, status, password_hash, is_admin, is_service_account, "
        "must_change_password, is_anonymous) "
        "VALUES (:login, :email, :login, 'active', :password_hash, :is_admin, false, false, false)",
        login=login,
        email=email,
        password_hash=password_hash,
        is_admin=is_admin,
    )


async def test_upgrade_and_downgrade() -> None:
    await _cleanup()
    try:
        _alembic("downgrade", "0044")
        assert await _tables() == set()
        assert not await _has_inert_check()
        await _seed_before_upgrade()

        _alembic("upgrade", "0045")
        assert await _tables() == set(_TABLES)
        assert await _has_inert_check()
        assert await _patterns() == {_PRESET: _NEW, f"{_PRESET}-other": _OTHER, "mig0045-schema": _NEW}
        assert await _execute("SELECT key FROM settings WHERE key = ANY(:keys)", keys=list(_HOOK_SETTINGS)) == []
        [git] = await _execute(
            "SELECT email, password_hash, is_admin, is_service_account, status FROM users WHERE login = '$git'"
        )
        assert tuple(git) == ("git@specivo.invalid", None, False, True, "active")

        _alembic("downgrade", "0044")
        assert await _tables() == set()
        assert not await _has_inert_check()
        assert await _patterns() == {_PRESET: _OLD, f"{_PRESET}-other": _OTHER, "mig0045-schema": _OLD}
        # The deleted secrets belonged to endpoints that no longer exist: they are not restored.
        assert await _execute("SELECT key FROM settings WHERE key = ANY(:keys)", keys=list(_HOOK_SETTINGS)) == []
        # The account goes unless journals still reference it; either way upgrading again works.
        _alembic("upgrade", "0045")
        assert len(await _execute("SELECT id FROM users WHERE login = '$git'")) == 1
    finally:
        _alembic("upgrade", "head")
        await _cleanup()


async def test_an_account_using_the_reserved_login_or_email_stops_the_upgrade() -> None:
    await _cleanup()
    try:
        _alembic("downgrade", "0044")
        await _insert_user("mig45_clash", "GIT@specivo.invalid")

        result = _run_alembic("upgrade", "0045")
        assert result.returncode != 0
        assert "'mig45_clash'" in result.stderr
        assert "Rename that account and run the upgrade again" in result.stderr
        assert not await _has_inert_check()

        await _execute("DELETE FROM users WHERE login = 'mig45_clash'")
        _alembic("upgrade", "0045")
    finally:
        _alembic("upgrade", "head")
        await _cleanup()


async def test_dollar_logins_with_a_password_or_admin_rights_stop_the_upgrade_readably() -> None:
    """An import could have created them; the error names them instead of a bare CHECK violation."""
    await _cleanup()
    try:
        _alembic("downgrade", "0044")
        await _insert_user("$mig45_import", "mig45-import@example.com", password_hash="argon2-hash")
        await _insert_user("$mig45_admin", "mig45-admin@example.com", is_admin=True)

        result = _run_alembic("upgrade", "0045")
        assert result.returncode != 0
        assert "ck_users_system_principal_inert" not in result.stderr
        assert "'$mig45_import' (has a password)" in result.stderr
        assert "'$mig45_admin' (is an administrator)" in result.stderr
        assert "run the upgrade again" in result.stderr
        assert not await _has_inert_check()

        await _execute(
            "UPDATE users SET password_hash = NULL, is_admin = false WHERE login = ANY(:logins)",
            logins=["$mig45_import", "$mig45_admin"],
        )
        _alembic("upgrade", "0045")
        assert await _has_inert_check()
    finally:
        _alembic("upgrade", "head")
        await _cleanup()
