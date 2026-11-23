"""Migration 0077 adds the billing method and the requested cost type of AI usage entries, and round-trips.

An entry charged to a subscription points at that subscription's billing
account with its payer and the method ``subscription``, and asked for no cost
type; any other entry keeps no account and ``none``, and asked for its cost
type unless that is ``unknown``. The downgrade drops both columns.

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
_LOGIN = "migbm_alice"


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
        "DELETE FROM ai_usage_entries WHERE user_id IN (SELECT id FROM users WHERE login = :login)", login=_LOGIN
    )
    await _execute("DELETE FROM users WHERE login = :login", login=_LOGIN)


async def _columns() -> list[str]:
    rows = await _execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'ai_usage_entries' "
        "AND column_name IN ('billing_method', 'requested_cost_type') ORDER BY column_name"
    )
    return [row[0] for row in rows]


async def _entry(model: str, cost_type: str, with_subscription: bool) -> None:
    await _execute(
        "INSERT INTO ai_usage_entries (user_id, reporter_user_id, source, tool_slug, model, usage_date, priced_at,"
        " cost_type, subscription_id) "
        "SELECT u.id, u.id, 'manual', 'claude-code', :model, DATE '2026-01-15', TIMESTAMPTZ '2026-01-15', :cost_type, "
        "CASE WHEN :with_sub THEN (SELECT s.id FROM ai_subscriptions s WHERE s.user_id = u.id) END "
        "FROM users u WHERE u.login = :login",
        model=model,
        cost_type=cost_type,
        with_sub=with_subscription,
        login=_LOGIN,
    )


async def test_entries_get_their_billing_method_and_requested_cost_type_and_the_downgrade_round_trips() -> None:
    await _cleanup()
    try:
        _alembic("downgrade", "0076")
        assert await _columns() == []
        await _execute(
            "INSERT INTO users (login, email, display_name) VALUES (:login, :email, 'M')",
            login=_LOGIN,
            email=f"{_LOGIN}@example.com",
        )
        await _execute(
            "WITH acc AS ("
            " INSERT INTO ai_billing_accounts (slug, name, kind, owner_user_id, payer, default_cost_type)"
            " SELECT 'claude-pro-x', 'Claude Pro', 'subscription', id, 'employee', 'subscription'"
            " FROM users WHERE login = :login RETURNING id, owner_user_id)"
            " INSERT INTO ai_subscriptions (user_id, provider, plan_key, plan_name, tool_slugs, amount, currency_code,"
            " billing_period, starts_on, created_by_id, billing_account_id)"
            " SELECT owner_user_id, 'anthropic', 'claude-pro', 'Claude Pro', '{claude-code}', 20, 'USD', 'month',"
            " DATE '2026-01-01', owner_user_id, id FROM acc",
            login=_LOGIN,
        )
        await _entry("mig-covered", "subscription", True)
        await _entry("mig-api", "api", False)
        await _entry("mig-unknown", "unknown", False)

        _alembic("upgrade", "0077")
        assert await _columns() == ["billing_method", "requested_cost_type"]
        rows = await _execute(
            "SELECT e.model, e.billing_method, e.requested_cost_type, e.payer, a.slug FROM ai_usage_entries e "
            "JOIN users u ON u.id = e.user_id LEFT JOIN ai_billing_accounts a ON a.id = e.billing_account_id "
            "WHERE u.login = :login ORDER BY e.model",
            login=_LOGIN,
        )
        assert rows == [
            ("mig-api", "none", "api", "unknown", None),
            ("mig-covered", "subscription", None, "employee", "claude-pro-x"),
            ("mig-unknown", "none", None, "unknown", None),
        ]

        _alembic("downgrade", "0076")
        assert await _columns() == []
        [(kept,)] = await _execute(
            "SELECT count(*) FROM ai_usage_entries e JOIN users u ON u.id = e.user_id "
            "WHERE u.login = :login AND e.billing_account_id IS NOT NULL",
            login=_LOGIN,
        )
        assert kept == 1
    finally:
        _alembic("upgrade", "head")
        await _cleanup()
