"""Monthly partitions of ``security_audit_logs``: created ahead, pruned after the retention period.

Each partition covers one calendar month and is named
``security_audit_logs_YYYY_MM``. A default partition catches rows no monthly
partition covers (for example on a fresh install, before the first run).

- :func:`ensure_partitions` creates the current month and the next months.
  A month whose rows already sit in the default partition is created
  standalone, the rows are moved in, and it is then attached: PostgreSQL
  refuses ``CREATE TABLE ... PARTITION OF`` while the default partition holds
  rows for the range. Each month runs in its own savepoint, so one failure
  does not stop the others.
- :func:`prune_partitions` detaches and drops monthly partitions that ended
  before the retention cutoff and deletes older rows from the default
  partition.

Both wait at most ``lock_timeout`` for a lock on the parent table and give up
otherwise; the daily task tries again the next day.
"""

from __future__ import annotations

import logging
import re
from datetime import date

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

PARENT_TABLE = "security_audit_logs"
DEFAULT_PARTITION = "security_audit_logs_default"
_MONTHLY_PARTITION = re.compile(r"^security_audit_logs_(\d{4})_(\d{2})$")
#: How long a partition change waits for a lock on the table before giving up.
LOCK_TIMEOUT = "5s"


def _add_months(day: date, months: int) -> date:
    """The first day of the month *months* after (or before) *day*'s month."""
    index = day.year * 12 + day.month - 1 + months
    return date(index // 12, index % 12 + 1, 1)


def _partition_name(month: date) -> str:
    return f"{PARENT_TABLE}_{month.year}_{month.month:02d}"


async def _exists(session: AsyncSession, name: str) -> bool:
    return bool(await session.scalar(text("SELECT to_regclass(:name) IS NOT NULL"), {"name": name}))


async def _create_month(session: AsyncSession, name: str, start: date, end: date) -> int:
    """Create the partition for [start, end); return how many rows moved out of the default partition."""
    bounds = f"FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')"
    # Literals, like the partition bounds: both read in the database's timezone. A bound
    # parameter would be read in this process's, and could miss a row near the month's edge.
    in_range = f"created_at >= '{start.isoformat()}' AND created_at < '{end.isoformat()}'"
    waiting = await session.scalar(
        text(f"SELECT count(*) FROM {DEFAULT_PARTITION} WHERE {in_range}"),  # noqa: S608 - constant names
    )
    if not waiting:
        await session.execute(text(f"CREATE TABLE {name} PARTITION OF {PARENT_TABLE} {bounds}"))
        return 0
    await session.execute(text(f"CREATE TABLE {name} (LIKE {PARENT_TABLE} INCLUDING DEFAULTS INCLUDING CONSTRAINTS)"))
    await session.execute(
        text(
            f"WITH moved AS (DELETE FROM {DEFAULT_PARTITION} WHERE {in_range} RETURNING *) "  # noqa: S608
            f"INSERT INTO {name} SELECT * FROM moved"
        )
    )
    await session.execute(text(f"ALTER TABLE {PARENT_TABLE} ATTACH PARTITION {name} {bounds}"))
    return int(waiting)


async def ensure_partitions(
    session: AsyncSession,
    months_ahead: int = 3,
    *,
    today: date | None = None,
) -> list[str]:
    """Create the monthly partitions for the current month plus *months_ahead* months.

    Returns the names of the partitions created. A month that fails (a lock
    it cannot get, an overlapping table) is logged and skipped; the next run
    tries it again. The caller commits.
    """
    first = _add_months(today or date.today(), 0)
    created: list[str] = []
    for offset in range(months_ahead + 1):
        start = _add_months(first, offset)
        end = _add_months(first, offset + 1)
        name = _partition_name(start)
        if await _exists(session, name):
            continue
        try:
            async with session.begin_nested():
                await session.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
                moved = await _create_month(session, name, start, end)
        except Exception as exc:  # noqa: BLE001 - one month must not stop the others
            logger.warning("Could not create audit partition %s: %s", name, exc)
            continue
        created.append(name)
        if moved:
            logger.info("Created audit partition %s and moved %d rows into it from the default partition", name, moved)
        else:
            logger.info("Created audit partition %s", name)
    return created


async def prune_partitions(
    session: AsyncSession,
    months: int,
    *,
    today: date | None = None,
) -> list[str]:
    """Drop audit data older than *months* whole months before the current one.

    The cutoff is the first day of the current month minus *months* months:
    with 12, everything before the same month a year ago goes. A monthly
    partition goes when its upper bound is at or before the cutoff; rows
    older than the cutoff in the default partition are deleted. ``0`` keeps
    everything. Returns the names of the partitions dropped. The caller commits.
    """
    if months <= 0:
        return []
    cutoff = _add_months(today or date.today(), -months)
    names = (
        await session.scalars(
            text(
                "SELECT child.relname FROM pg_inherits "
                "JOIN pg_class child ON child.oid = pg_inherits.inhrelid "
                "JOIN pg_class parent ON parent.oid = pg_inherits.inhparent "
                "WHERE parent.relname = :parent ORDER BY child.relname"
            ),
            {"parent": PARENT_TABLE},
        )
    ).all()
    dropped: list[str] = []
    for name in names:
        match = _MONTHLY_PARTITION.match(name)
        if match is None:
            continue
        upper = _add_months(date(int(match[1]), int(match[2]), 1), 1)
        if upper > cutoff:
            continue
        try:
            async with session.begin_nested():
                await session.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
                await session.execute(text(f"ALTER TABLE {PARENT_TABLE} DETACH PARTITION {name}"))
                await session.execute(text(f"DROP TABLE {name}"))
        except Exception as exc:  # noqa: BLE001 - retried by the next daily run
            logger.warning("Could not drop audit partition %s, will retry tomorrow: %s", name, exc)
            continue
        dropped.append(name)
        logger.info("Dropped audit partition %s (retention %d months)", name, months)
    try:
        async with session.begin_nested():
            await session.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
            result = await session.execute(
                text(f"DELETE FROM {DEFAULT_PARTITION} WHERE created_at < :cutoff"),  # noqa: S608 - constant name
                {"cutoff": cutoff},
            )
    except Exception as exc:  # noqa: BLE001 - retried by the next daily run
        logger.warning("Could not prune the default audit partition, will retry tomorrow: %s", exc)
    else:
        deleted = getattr(result, "rowcount", 0) or 0
        if deleted:
            logger.info("Deleted %d audit rows older than %s from the default partition", deleted, cutoff)
    return dropped
