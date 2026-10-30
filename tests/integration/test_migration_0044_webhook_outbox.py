"""Migration 0044 moves webhook secrets to secret_box and turns deliveries into an outbox.

Runs Alembic in a subprocess against the test database, so it changes schema
shared by every test and is marked ``serial``. The upgrade always ends at head,
even when an assertion fails half-way. Rows it inserts are committed for real
(Alembic runs on its own connection) and deleted at the end.
"""

from __future__ import annotations

import base64
import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from specivo.core import secret_box
from specivo.core.config import get_settings

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration, pytest.mark.serial]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROJECT_KEY = "MIGWH"
_NEW_COLUMNS = {"delivery_uuid", "status", "next_attempt_at", "last_error"}


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
    rows = await _execute("SELECT column_name FROM information_schema.columns WHERE table_name = :t", t=table)
    return {row[0] for row in rows}


def _legacy_encrypt(raw: str) -> str:
    key = base64.urlsafe_b64encode(hashlib.sha256(get_settings().secret_key.encode()).digest())
    return Fernet(key).encrypt(raw.encode()).decode()


async def _cleanup() -> None:
    await _execute("DELETE FROM projects WHERE key = :key", key=_PROJECT_KEY)


async def _secrets() -> dict[str, str]:
    rows = await _execute(
        "SELECT w.url, w.secret FROM webhooks w JOIN projects p ON p.id = w.project_id WHERE p.key = :key",
        key=_PROJECT_KEY,
    )
    return {row[0]: row[1] for row in rows}


async def test_secrets_reencrypted_and_deliveries_backfilled() -> None:
    assert _NEW_COLUMNS <= await _columns("webhook_deliveries")
    await _cleanup()

    try:
        _alembic("downgrade", "0043")
        assert not (_NEW_COLUMNS & await _columns("webhook_deliveries"))
        assert "success" in await _columns("webhook_deliveries")

        await _execute(
            "INSERT INTO projects (name, key, identifier, path, status, is_public, settings, issue_sequence) "
            "VALUES ('Migration webhooks', :key, 'migwh', 'migwh', 1, false, '{}', 0)",
            key=_PROJECT_KEY,
        )
        for url, secret in (
            ("https://hooks.example.com/old", _legacy_encrypt("old-style-secret")),
            ("https://hooks.example.com/plain", "plaintext-secret"),
        ):
            await _execute(
                "INSERT INTO webhooks (project_id, url, secret, events, is_active) "
                "SELECT id, :url, :secret, '[\"issue.created\"]', true FROM projects WHERE key = :key",
                url=url,
                secret=secret,
                key=_PROJECT_KEY,
            )
        for success in (True, False):
            await _execute(
                "INSERT INTO webhook_deliveries (webhook_id, event, payload, attempts, success) "
                "SELECT w.id, 'issue.created', '{}', 1, :success FROM webhooks w "
                "JOIN projects p ON p.id = w.project_id WHERE p.key = :key AND w.url LIKE '%/old'",
                success=success,
                key=_PROJECT_KEY,
            )

        _alembic("upgrade", "0044")

        secrets = await _secrets()
        assert secret_box.decrypt(secrets["https://hooks.example.com/old"], "webhook-secret") == "old-style-secret"
        assert secret_box.decrypt(secrets["https://hooks.example.com/plain"], "webhook-secret") == "plaintext-secret"

        rows = await _execute(
            "SELECT d.status, d.delivery_uuid FROM webhook_deliveries d JOIN webhooks w ON w.id = d.webhook_id "
            "JOIN projects p ON p.id = w.project_id WHERE p.key = :key ORDER BY d.id",
            key=_PROJECT_KEY,
        )
        assert [row[0] for row in rows] == ["succeeded", "failed"]
        assert len({row[1] for row in rows}) == 2
        include_private = await _execute(
            "SELECT DISTINCT w.include_private FROM webhooks w JOIN projects p ON p.id = w.project_id "
            "WHERE p.key = :key",
            key=_PROJECT_KEY,
        )
        assert include_private == [(False,)]

        _alembic("downgrade", "0043")
        legacy = await _secrets()
        key = base64.urlsafe_b64encode(hashlib.sha256(get_settings().secret_key.encode()).digest())
        assert Fernet(key).decrypt(legacy["https://hooks.example.com/old"].encode()).decode() == "old-style-secret"
        successes = await _execute(
            "SELECT d.success FROM webhook_deliveries d JOIN webhooks w ON w.id = d.webhook_id "
            "JOIN projects p ON p.id = w.project_id WHERE p.key = :key ORDER BY d.id",
            key=_PROJECT_KEY,
        )
        assert [row[0] for row in successes] == [True, False]
    finally:
        _alembic("upgrade", "head")
        await _cleanup()
