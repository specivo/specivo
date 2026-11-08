"""Audit log partitions against PostgreSQL: created ahead, rows moved out of the default, pruned.

Serial: the partition DDL locks the shared ``security_audit_logs`` parent.
Each test runs in a transaction of its own that is rolled back, so the tables
it creates or drops come back as they were. The usual ``db_session`` is not
used: its teardown deletes audit rows from another connection, which would
wait on this transaction's locks.

Months far from today are used so no real partition is in the way.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from specivo.tasks.partition_management import ensure_partitions, prune_partitions

pytestmark = [pytest.mark.integration, pytest.mark.serial]


@pytest_asyncio.fixture
async def session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with db_engine.connect() as conn:
        transaction = await conn.begin()
        try:
            yield AsyncSession(bind=conn, expire_on_commit=False)
        finally:
            await transaction.rollback()


async def _insert(session: AsyncSession, at: datetime, marker: str) -> None:
    await session.execute(
        text("INSERT INTO security_audit_logs (event_type, details, created_at) VALUES ('sessions_revoked', :d, :at)"),
        {"d": f'{{"marker": "{marker}"}}', "at": at},
    )


async def _home(session: AsyncSession, marker: str) -> str | None:
    """The partition holding the row with *marker*."""
    return await session.scalar(
        text("SELECT tableoid::regclass::text FROM security_audit_logs WHERE details->>'marker' = :m"), {"m": marker}
    )


async def _exists(session: AsyncSession, name: str) -> bool:
    return bool(await session.scalar(text("SELECT to_regclass(:n) IS NOT NULL"), {"n": name}))


async def _is_partition(session: AsyncSession, name: str) -> bool:
    return bool(
        await session.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM pg_inherits WHERE inhrelid = to_regclass(:n) "
                "AND inhparent = 'security_audit_logs'::regclass)"
            ),
            {"n": name},
        )
    )


async def test_ensure_partitions_creates_the_current_month_and_the_next_three(session: AsyncSession) -> None:
    created = await ensure_partitions(session, today=date(2031, 11, 15))

    assert created == [f"security_audit_logs_{m}" for m in ("2031_11", "2031_12", "2032_01", "2032_02")]
    assert all([await _is_partition(session, name) for name in created])
    assert await ensure_partitions(session, today=date(2031, 11, 15)) == []


async def test_ensure_partitions_moves_rows_out_of_the_default_partition(session: AsyncSession) -> None:
    await _insert(session, datetime(2031, 5, 15, 12, tzinfo=UTC), "early-row")
    await _insert(session, datetime(2031, 9, 2, tzinfo=UTC), "later-row")
    assert await _home(session, "early-row") == "security_audit_logs_default"

    created = await ensure_partitions(session, months_ahead=0, today=date(2031, 5, 10))

    assert created == ["security_audit_logs_2031_05"]
    assert await _is_partition(session, "security_audit_logs_2031_05")
    assert await _home(session, "early-row") == "security_audit_logs_2031_05"
    assert await _home(session, "later-row") == "security_audit_logs_default"
    # The moved table carries the parent's indexes, primary key included, once attached.
    indexes = await session.scalar(
        text("SELECT count(*) FROM pg_indexes WHERE tablename = 'security_audit_logs_2031_05'")
    )
    parent_indexes = await session.scalar(
        text("SELECT count(*) FROM pg_indexes WHERE tablename = 'security_audit_logs'")
    )
    assert indexes == parent_indexes
    # New rows for the month land in it.
    await _insert(session, datetime(2031, 5, 20, tzinfo=UTC), "new-row")
    assert await _home(session, "new-row") == "security_audit_logs_2031_05"


async def test_ensure_partitions_one_failed_month_does_not_stop_the_rest(session: AsyncSession) -> None:
    # A table already covering part of June makes June's partition fail (overlap).
    await session.execute(
        text(
            "CREATE TABLE security_audit_logs_overlap PARTITION OF security_audit_logs "
            "FOR VALUES FROM ('2031-06-10') TO ('2031-06-20')"
        )
    )

    created = await ensure_partitions(session, months_ahead=2, today=date(2031, 5, 1))

    assert created == ["security_audit_logs_2031_05", "security_audit_logs_2031_07"]
    assert not await _exists(session, "security_audit_logs_2031_06")


async def test_prune_drops_old_partitions_only(session: AsyncSession) -> None:
    await ensure_partitions(session, months_ahead=2, today=date(2029, 9, 1))
    await _insert(session, datetime(2029, 9, 5, tzinfo=UTC), "in-september")
    await _insert(session, datetime(2029, 6, 15, tzinfo=UTC), "old-default")
    await _insert(session, datetime(2029, 12, 15, tzinfo=UTC), "recent-default")

    # Cutoff: 2029-11-01, twelve months before November 2030.
    dropped = await prune_partitions(session, 12, today=date(2030, 11, 4))

    assert {"security_audit_logs_2029_09", "security_audit_logs_2029_10"} <= set(dropped)
    assert "security_audit_logs_2029_11" not in dropped
    assert all(name < "security_audit_logs_2029_11" for name in dropped)
    assert not await _exists(session, "security_audit_logs_2029_09")
    assert await _is_partition(session, "security_audit_logs_2029_11")
    assert await _exists(session, "security_audit_logs_default")
    assert await _home(session, "in-september") is None
    assert await _home(session, "old-default") is None
    assert await _home(session, "recent-default") == "security_audit_logs_default"


async def test_prune_zero_months_keeps_everything(session: AsyncSession) -> None:
    await ensure_partitions(session, months_ahead=0, today=date(2020, 1, 1))
    await _insert(session, datetime(2020, 1, 5, tzinfo=UTC), "kept-partition")
    await _insert(session, datetime(2019, 6, 5, tzinfo=UTC), "kept-default")

    assert await prune_partitions(session, 0, today=date(2030, 11, 4)) == []

    assert await _home(session, "kept-partition") == "security_audit_logs_2020_01"
    assert await _home(session, "kept-default") == "security_audit_logs_default"
