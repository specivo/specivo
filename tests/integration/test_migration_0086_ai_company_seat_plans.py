"""Migration 0086 adds company seat plans (no holder, seats, members) and round-trips.

Existing subscriptions stay personal. After the upgrade a plan without a
holder needs a seat count and may cost nothing, while a personal plan still
costs something; the downgrade deletes the company plans and their members and
restores the old checks.

Runs Alembic in a subprocess against the test database, so it changes schema
shared by every test and is marked ``serial``. The upgrade always ends at head,
even when an assertion fails half-way. Rows it inserts are committed for real
(Alembic runs on its own connection) and deleted at the end.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration, pytest.mark.serial]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION = _REPO_ROOT / "alembic" / "versions" / "0086_ai_company_seat_plans.py"
_LOGINS = ("migseat_alice", "migseat_bob")


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
        "DELETE FROM ai_subscriptions WHERE plan_name LIKE 'Migseat %' OR user_id IN "
        "(SELECT id FROM users WHERE login = ANY(:logins))",
        logins=list(_LOGINS),
    )
    await _execute("DELETE FROM users WHERE login = ANY(:logins)", logins=list(_LOGINS))


async def _table_exists() -> bool:
    [(name,)] = await _execute("SELECT to_regclass('public.ai_subscription_members')::text")
    return name is not None


async def _user_id_nullable() -> bool:
    [(nullable,)] = await _execute(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_name = 'ai_subscriptions' AND column_name = 'user_id'"
    )
    return nullable == "YES"


_INSERT_PLAN = (
    "INSERT INTO ai_subscriptions (user_id, seat_count, provider, plan_key, plan_name, amount, currency_code,"
    " billing_period, starts_on) VALUES ({user}, {seats}, 'anthropic', 'custom', :name, :amount, 'USD', 'month',"
    " DATE '2026-11-01') RETURNING id"
)
#: The same before the migration (and after its downgrade): no seat count.
_INSERT_OLD_PLAN = _INSERT_PLAN.replace("seat_count, ", "").replace("{seats}, ", "")
_ALICE = "(SELECT id FROM users WHERE login = 'migseat_alice')"


async def test_company_plans_are_added_and_the_downgrade_round_trips() -> None:
    revision, down = _revisions()
    await _cleanup()
    try:
        _alembic("downgrade", down)
        assert not await _table_exists() and not await _user_id_nullable()
        for login in _LOGINS:
            await _execute(
                "INSERT INTO users (login, email, display_name) VALUES (:login, :email, 'M')",
                login=login,
                email=f"{login}@example.com",
            )
        await _execute(_INSERT_OLD_PLAN.format(user=_ALICE), name="Migseat personal", amount=20)

        _alembic("upgrade", revision)
        assert await _table_exists() and await _user_id_nullable()
        # The personal plan is untouched.
        [(user_id, seats)] = await _execute(
            "SELECT s.user_id, s.seat_count FROM ai_subscriptions s WHERE s.plan_name = 'Migseat personal'"
        )
        assert user_id is not None and seats is None

        # A company plan: no holder, seats, a zero fee allowed; members by date.
        [(plan_id,)] = await _execute(_INSERT_PLAN.format(user="NULL", seats="3"), name="Migseat team", amount=0)
        await _execute(
            "INSERT INTO ai_subscription_members (subscription_id, user_id, starts_on, ends_on) "
            "SELECT :plan, id, DATE '2026-11-01', DATE '2026-11-30' FROM users WHERE login = 'migseat_bob'",
            plan=plan_id,
        )
        for user, seats, amount in (
            ("NULL", "NULL", 10),  # no holder needs seats
            ("NULL", "0", 10),  # at least one seat
            (_ALICE, "2", 10),  # a person's plan has none
            (_ALICE, "NULL", 0),  # and costs something
        ):
            with pytest.raises(IntegrityError):
                await _execute(_INSERT_PLAN.format(user=user, seats=seats), name="Migseat bad", amount=amount)
        with pytest.raises(IntegrityError):
            await _execute(
                "INSERT INTO ai_subscription_members (subscription_id, user_id, starts_on, ends_on) "
                "SELECT :plan, id, DATE '2026-11-30', DATE '2026-11-01' FROM users WHERE login = 'migseat_bob'",
                plan=plan_id,
            )
        # Deleting a person deletes their seats.
        await _execute("DELETE FROM users WHERE login = 'migseat_bob'")
        assert await _execute("SELECT 1 FROM ai_subscription_members WHERE subscription_id = :p", p=plan_id) == []

        _alembic("downgrade", down)
        assert not await _table_exists() and not await _user_id_nullable()
        assert await _execute("SELECT 1 FROM ai_subscriptions WHERE plan_name = 'Migseat team'") == []
        assert len(await _execute("SELECT 1 FROM ai_subscriptions WHERE plan_name = 'Migseat personal'")) == 1
        with pytest.raises(IntegrityError):
            await _execute(_INSERT_OLD_PLAN.format(user=_ALICE), name="Migseat zero", amount=0)
    finally:
        _alembic("upgrade", "head")
        await _cleanup()
