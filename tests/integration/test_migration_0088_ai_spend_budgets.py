"""Migration 0088 adds AI spend budgets and their alerts, and round-trips.

After the upgrade a budget covers one account or one payer, never both or
neither, at most one per account and per payer, with an amount above zero and
1 to 5 thresholds from 1 to 1000; an alert is unique per budget, month and
threshold and goes with its budget. The downgrade drops both tables.

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
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration, pytest.mark.serial]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION = _REPO_ROOT / "alembic" / "versions" / "0088_ai_spend_budgets.py"
_SLUG = "migbud-gateway"


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


async def _tables() -> set[str]:
    rows = await _execute(
        "SELECT tablename FROM pg_tables WHERE tablename IN ('ai_spend_budgets', 'ai_spend_budget_alerts')"
    )
    return {r[0] for r in rows}


async def _cleanup() -> None:
    if "ai_spend_budgets" in await _tables():
        await _execute(
            "DELETE FROM ai_spend_budgets WHERE payer IS NOT NULL OR billing_account_id IN "
            "(SELECT id FROM ai_billing_accounts WHERE slug = :slug)",
            slug=_SLUG,
        )
    await _execute("DELETE FROM ai_billing_accounts WHERE slug = :slug AND owner_user_id IS NULL", slug=_SLUG)


_BUDGET = (
    "INSERT INTO ai_spend_budgets (billing_account_id, payer, amount_amount, amount_currency{extra_cols}) "
    "VALUES ({account}, {payer}, :amount, 'USD'{extra_vals}) RETURNING id"
)


async def _budget(account: str = "NULL", payer: str = "NULL", amount: str = "100", thresholds: str | None = None):
    extra_cols, extra_vals = ("", "") if thresholds is None else (", thresholds", f", '{thresholds}'::integer[]")
    sql = _BUDGET.format(account=account, payer=payer, extra_cols=extra_cols, extra_vals=extra_vals)
    return await _execute(sql, amount=amount)


async def test_budgets_and_alerts_are_added_and_the_downgrade_round_trips() -> None:
    revision, down = _revisions()
    await _cleanup()
    try:
        _alembic("downgrade", down)
        assert await _tables() == set()
        [(account_id,)] = await _execute(
            "INSERT INTO ai_billing_accounts (slug, name, kind, payer) VALUES (:slug, 'Migbud', 'gateway', "
            "'employer') RETURNING id",
            slug=_SLUG,
        )

        _alembic("upgrade", revision)
        assert await _tables() == {"ai_spend_budgets", "ai_spend_budget_alerts"}
        [(budget_id,)] = await _budget(account=str(account_id))
        [(thresholds, active)] = await _execute(
            "SELECT thresholds, is_active FROM ai_spend_budgets WHERE id = :id", id=budget_id
        )
        assert thresholds == [80, 100] and active is True
        await _budget(payer="'employer'", thresholds="{50}")

        for kwargs in (
            {"account": str(account_id)},  # one budget per account
            {"payer": "'employer'"},  # one per payer
            {"account": str(account_id), "payer": "'employee'"},  # not both
            {},  # not neither
            {"payer": "'unknown'"},
            {"payer": "'employee'", "amount": "0"},
            {"payer": "'employee'", "thresholds": "{}"},
            {"payer": "'employee'", "thresholds": "{0}"},
            {"payer": "'employee'", "thresholds": "{1001}"},
            {"payer": "'employee'", "thresholds": "{1,2,3,4,5,6}"},
        ):
            with pytest.raises(IntegrityError):
                await _budget(**kwargs)

        alert = (
            "INSERT INTO ai_spend_budget_alerts (budget_id, period_start, threshold, spend_amount, spend_currency,"
            " budget_amount) VALUES (:b, :day, 80, 85, 'USD', 100)"
        )
        await _execute(alert, b=budget_id, day=date(2026, 3, 1))
        with pytest.raises(IntegrityError):
            await _execute(alert, b=budget_id, day=date(2026, 3, 1))  # once per month and threshold
        with pytest.raises(IntegrityError):
            await _execute(alert, b=budget_id, day=date(2026, 3, 2))  # a month is its first day
        await _execute(alert, b=budget_id, day=date(2026, 4, 1))
        # Deleting the account deletes its budget, and the budget its alerts.
        await _execute("DELETE FROM ai_billing_accounts WHERE id = :id", id=account_id)
        assert await _execute("SELECT 1 FROM ai_spend_budget_alerts WHERE budget_id = :b", b=budget_id) == []

        _alembic("downgrade", down)
        assert await _tables() == set()
    finally:
        _alembic("upgrade", "head")
        await _cleanup()
