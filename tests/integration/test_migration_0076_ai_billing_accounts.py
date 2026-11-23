"""Migration 0076 adds AI billing accounts, links every existing subscription to one, and round-trips.

Existing subscriptions each get a personal ``subscription`` account paid by
the employee; existing usage entries get ``payer`` ``unknown`` and no account.
The downgrade drops the new columns and the table, and keeps the
subscriptions and entries.

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
_LOGINS = ("migbill_alice", "migbill_bob")


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


async def _cleanup() -> None:
    await _execute(
        "DELETE FROM ai_usage_entries WHERE user_id IN (SELECT id FROM users WHERE login = ANY(:logins))",
        logins=list(_LOGINS),
    )
    await _execute("DELETE FROM users WHERE login = ANY(:logins)", logins=list(_LOGINS))


async def _table_exists() -> bool:
    [(name,)] = await _execute("SELECT to_regclass('public.ai_billing_accounts')::text")
    return name is not None


async def _columns(table: str, names: list[str]) -> list[str]:
    rows = await _execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = :table AND column_name = ANY(:names) ORDER BY column_name",
        table=table,
        names=names,
    )
    return [row[0] for row in rows]


async def _add_subscription(login: str, plan_key: str, plan_name: str) -> None:
    await _execute(
        "INSERT INTO ai_subscriptions (user_id, provider, plan_key, plan_name, tool_slugs, amount, currency_code,"
        " billing_period, starts_on, created_by_id) "
        "SELECT id, 'anthropic', :plan_key, :plan_name, '{claude-code}', 20, 'USD', 'month', DATE '2026-01-01', id "
        "FROM users WHERE login = :login",
        login=login,
        plan_key=plan_key,
        plan_name=plan_name,
    )


async def test_subscriptions_are_backfilled_with_accounts_and_the_downgrade_round_trips() -> None:
    await _cleanup()
    try:
        _alembic("downgrade", "0075")
        assert not await _table_exists()
        for login in _LOGINS:
            await _execute(
                "INSERT INTO users (login, email, display_name) VALUES (:login, :email, 'M')",
                login=login,
                email=f"{login}@example.com",
            )
        await _add_subscription("migbill_alice", "claude-pro", "Claude Pro")
        await _add_subscription("migbill_alice", "claude-max-5x", "Claude Max 5x")
        await _add_subscription("migbill_bob", "claude-pro", "Claude Pro")
        await _execute(
            "INSERT INTO ai_usage_entries (user_id, reporter_user_id, source, tool_slug, model, usage_date, priced_at) "
            "SELECT id, id, 'manual', 'claude-code', 'lab-model-1', DATE '2026-01-15', TIMESTAMPTZ '2026-01-15' "
            "FROM users WHERE login = 'migbill_alice'"
        )

        _alembic("upgrade", "0076")
        assert await _table_exists()
        assert await _columns("ai_usage_entries", ["billing_account_id", "payer", "route"]) == [
            "billing_account_id",
            "payer",
            "route",
        ]
        rows = await _execute(
            "SELECT u.login, s.plan_key, a.slug = s.plan_key || '-' || s.id, a.name, a.kind, a.payer,"
            " a.owner_user_id = s.user_id, a.created_by_id = s.created_by_id, a.reimburse_share,"
            " a.route_patterns, a.tool_slugs, a.default_cost_type, a.is_active "
            "FROM ai_subscriptions s JOIN users u ON u.id = s.user_id "
            "JOIN ai_billing_accounts a ON a.id = s.billing_account_id "
            "WHERE u.login = ANY(:logins) ORDER BY u.login, s.plan_key",
            logins=list(_LOGINS),
        )
        assert rows == [
            ("migbill_alice", "claude-max-5x", True, "Claude Max 5x", "subscription", "employee", True, True, None,
             [], [], "subscription", True),
            ("migbill_alice", "claude-pro", True, "Claude Pro", "subscription", "employee", True, True, None,
             [], [], "subscription", True),
            ("migbill_bob", "claude-pro", True, "Claude Pro", "subscription", "employee", True, True, None,
             [], [], "subscription", True),
        ]  # fmt: skip
        [(unlinked,)] = await _execute("SELECT count(*) FROM ai_subscriptions WHERE billing_account_id IS NULL")
        assert unlinked == 0
        entries = await _execute(
            "SELECT e.payer, e.billing_account_id, e.route FROM ai_usage_entries e "
            "JOIN users u ON u.id = e.user_id WHERE u.login = 'migbill_alice'"
        )
        assert entries == [("unknown", None, None)]

        _alembic("downgrade", "0075")
        assert not await _table_exists()
        assert await _columns("ai_usage_entries", ["billing_account_id", "payer", "route"]) == []
        assert await _columns("ai_subscriptions", ["billing_account_id"]) == []
        [(subs,)] = await _execute(
            "SELECT count(*) FROM ai_subscriptions s JOIN users u ON u.id = s.user_id WHERE u.login = ANY(:logins)",
            logins=list(_LOGINS),
        )
        assert subs == 3
    finally:
        _alembic("upgrade", "head")
        await _cleanup()
