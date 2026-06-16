"""Partition auto-creation for security_audit_logs.

Creates the current month's partition plus N months ahead so the
partitioned table always has a target partition for incoming audit
events — including immediately after a fresh deploy mid-month.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def ensure_partitions(
    session: AsyncSession,
    months_ahead: int = 3,
) -> None:
    """Create monthly partitions for the current month plus N months ahead.

    Each partition covers a full calendar month with naming convention:
    ``security_audit_logs_YYYY_MM``

    The current month is always included so that audit inserts have a
    target partition immediately after a fresh deploy — without it,
    inserts would route to the default partition until the next
    monthly tick.

    Uses ``CREATE TABLE IF NOT EXISTS`` for idempotency — safe to run
    multiple times without error.

    Args:
        session: Async database session.
        months_ahead: Number of future months to create partitions for
            in addition to the current month. With ``months_ahead=3``,
            four partitions are created: the current month and the
            next three.
    """
    today = date.today()

    for i in range(0, months_ahead + 1):
        # Calculate the target month
        month = today.month + i
        year = today.year
        while month > 12:
            month -= 12
            year += 1

        # Calculate the start of the *next* month for the upper bound
        next_month = month + 1
        next_year = year
        if next_month > 12:
            next_month = 1
            next_year += 1

        partition_name = f"security_audit_logs_{year}_{month:02d}"
        start_date = f"{year}-{month:02d}-01"
        end_date = f"{next_year}-{next_month:02d}-01"

        sql = text(
            f"CREATE TABLE IF NOT EXISTS {partition_name} "
            f"PARTITION OF security_audit_logs "
            f"FOR VALUES FROM ('{start_date}') TO ('{end_date}')"
        )
        await session.execute(sql)
