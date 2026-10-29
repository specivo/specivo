"""Process-local cache for rarely-changing lookup tables.

Trackers, statuses, priorities, and time entry activities change only via
admin UI, so we cache them in process memory for the dropdowns that list
them. Call invalidate_lookups() from admin CRUD endpoints after any write.

The cache is process-local: invalidating it reaches only the worker that
made the change, and an import or another worker can create records too. So
a copy is kept for at most :data:`TTL_SECONDS`, and nothing that must be
exact reads from it. An issue's history names the records it refers to from
the database (``specivo.services.activity_lookups``).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass
class Lookups:
    trackers: list[Any] = field(default_factory=list)
    statuses: list[Any] = field(default_factory=list)
    priorities: list[Any] = field(default_factory=list)
    activities: list[Any] = field(default_factory=list)


# How long a worker keeps its copy before reading the tables again.
TTL_SECONDS = 60.0

_cache: Lookups | None = None
_loaded_at = 0.0


async def get_lookups(session: AsyncSession) -> Lookups:
    """Return cached lookups, loading them from the database when absent or older than :data:`TTL_SECONDS`."""
    global _cache, _loaded_at
    if _cache is not None and time.monotonic() - _loaded_at < TTL_SECONDS:
        return _cache

    # Imports inside function to avoid circular deps at module load
    from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
    from specivo.models.time_entry import TimeEntryActivity

    trackers_r = await session.execute(select(Tracker).order_by(Tracker.position))
    statuses_r = await session.execute(select(IssueStatus).order_by(IssueStatus.position))
    priorities_r = await session.execute(
        select(IssuePriority).where(IssuePriority.active.is_(True)).order_by(IssuePriority.position)
    )
    activities_r = await session.execute(
        select(TimeEntryActivity)
        .where(TimeEntryActivity.active.is_(True))
        .order_by(TimeEntryActivity.position)
    )

    _cache = Lookups(
        trackers=list(trackers_r.scalars().all()),
        statuses=list(statuses_r.scalars().all()),
        priorities=list(priorities_r.scalars().all()),
        activities=list(activities_r.scalars().all()),
    )
    _loaded_at = time.monotonic()
    return _cache


def invalidate_lookups() -> None:
    """Clear the cache. Call from admin CRUD endpoints that modify lookup data."""
    global _cache
    _cache = None
