"""Migration 0085 adds AI reimbursement claims and their frozen entries, and round-trips.

After the upgrade a claim and a frozen entry can be written; the month must
be a first day and the status a known one; a person has one claim per month
that is not rejected; deleting the usage entry keeps the frozen row with no
entry id; deleting the claim drops its entries; a user with a claim cannot be
deleted. The downgrade drops both tables. It steps back one revision rather
than naming the previous one, so it holds wherever the release chain puts it.

Runs Alembic in a subprocess against the test database, so it changes schema
shared by every test and is marked ``serial``. The upgrade always ends at head,
even when an assertion fails half-way. Rows it inserts are committed for real
(Alembic runs on its own connection) and deleted at the end.
"""

from __future__ import annotations

import os
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
_LOGIN = "mig_claimer"
_TABLES = ["ai_reimbursement_claim_entries", "ai_reimbursement_claims"]


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


async def _tables() -> list[str]:
    rows = await _execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_name IN ('ai_reimbursement_claims', 'ai_reimbursement_claim_entries') ORDER BY table_name"
    )
    return [row[0] for row in rows]


async def _cleanup() -> None:
    if await _tables():
        await _execute(
            "DELETE FROM ai_reimbursement_claims WHERE user_id IN (SELECT id FROM users WHERE login = :login)",
            login=_LOGIN,
        )
    await _execute(
        "DELETE FROM ai_usage_entries WHERE user_id IN (SELECT id FROM users WHERE login = :login)", login=_LOGIN
    )
    await _execute("DELETE FROM users WHERE login = :login", login=_LOGIN)


async def _claim(month: date, status: str = "draft") -> int:
    [(claim_id,)] = await _execute(
        "INSERT INTO ai_reimbursement_claims (user_id, month, status, total_amount, total_currency, computed_at) "
        "SELECT id, :month, :status, 47.5, 'USD', now() FROM users WHERE login = :login RETURNING id",
        month=month,
        status=status,
        login=_LOGIN,
    )
    return int(claim_id)


async def test_claims_and_their_entries_and_the_downgrade_round_trips() -> None:
    await _cleanup()
    try:
        _alembic("upgrade", "head")
        assert await _tables() == _TABLES
        _alembic("downgrade", "-1")
        assert await _tables() == []
        _alembic("upgrade", "head")
        assert await _tables() == _TABLES

        await _execute(
            "INSERT INTO users (login, email, display_name) VALUES (:login, :email, 'Claimer')",
            login=_LOGIN,
            email=f"{_LOGIN}@example.com",
        )
        [(entry_id,)] = await _execute(
            "INSERT INTO ai_usage_entries (user_id, reporter_user_id, source, tool_slug, model, usage_date, priced_at) "
            "SELECT id, id, 'manual', 'claude-code', 'mig-model', DATE '2026-03-05', TIMESTAMPTZ '2026-03-05 00:00Z' "
            "FROM users WHERE login = :login RETURNING id",
            login=_LOGIN,
        )
        claim_id = await _claim(date(2026, 3, 1))
        await _execute(
            "INSERT INTO ai_reimbursement_claim_entries (claim_id, entry_id, revision, fingerprint, usage_date, "
            "tool_slug, model, payer, cost_type, share, currency, marginal_amount, seat_amount, reimbursable_amount) "
            "VALUES (:cid, :eid, 1, :fp, DATE '2026-03-05', 'claude-code', 'mig-model', 'employee', 'api', 0.5, "
            "'USD', 10, 0, 5)",
            cid=claim_id,
            eid=entry_id,
            fp="f" * 64,
        )
        [(status,)] = await _execute("SELECT status FROM ai_reimbursement_claims WHERE id = :cid", cid=claim_id)
        assert status == "draft"

        # One claim per person and month that is not rejected; the month is a first day; known statuses only.
        with pytest.raises(IntegrityError):
            await _claim(date(2026, 3, 1))
        with pytest.raises(IntegrityError):
            await _claim(date(2026, 4, 2))
        with pytest.raises(IntegrityError):
            await _claim(date(2026, 5, 1), status="lost")
        await _execute("UPDATE ai_reimbursement_claims SET status = 'rejected' WHERE id = :cid", cid=claim_id)
        again = await _claim(date(2026, 3, 1))

        # A deleted usage entry stays in the claim, with no entry id; a user with a claim stays.
        await _execute("DELETE FROM ai_usage_entries WHERE id = :eid", eid=entry_id)
        [(linked,)] = await _execute(
            "SELECT entry_id FROM ai_reimbursement_claim_entries WHERE claim_id = :cid", cid=claim_id
        )
        assert linked is None
        with pytest.raises(IntegrityError):
            await _execute("DELETE FROM users WHERE login = :login", login=_LOGIN)

        # Deleting a claim drops its entries.
        await _execute("DELETE FROM ai_reimbursement_claims WHERE id IN (:a, :b)", a=claim_id, b=again)
        assert await _execute("SELECT 1 FROM ai_reimbursement_claim_entries WHERE claim_id = :cid", cid=claim_id) == []
    finally:
        _alembic("upgrade", "head")
        await _cleanup()
