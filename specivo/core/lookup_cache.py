"""Process-local cache for rarely-changing lookup tables.

Trackers, statuses, priorities, and time entry activities change only via
admin UI, so we cache them in process memory. Call invalidate_lookups()
from admin CRUD endpoints after any write.

Note: this cache is process-local. In multi-worker deployments, each worker
holds its own copy and stale data may persist for a few seconds after admin
edits until each worker's cache is invalidated (or the worker restarts).
"""

from __future__ import annotations

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


_cache: Lookups | None = None


async def get_lookups(session: AsyncSession) -> Lookups:
    """Return cached lookups, loading from DB on first call."""
    global _cache
    if _cache is not None:
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
    return _cache


def invalidate_lookups() -> None:
    """Clear the cache. Call from admin CRUD endpoints that modify lookup data."""
    global _cache
    _cache = None
