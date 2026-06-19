"""Integration tests for the recurring-task Celery beat poller.

These drive the async generation body directly against the test session,
bypassing Celery's broker and the synchronous task wrapper. The poller is
expected to be:

- correct for fixed mode (generates in-window occurrences);
- conservative for flexible mode with an open instance (generates nothing);
- skip disabled patterns entirely;
- fault-isolated (one pattern raising does not block the others);
- idempotent (a second run is a no-op).

One test exercises the full lock path through ``_generate_recurring_async`` by
binding the global session factory to the test connection, so the Redis lock
acquisition is actually run against the test Redis.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.issue import Issue
from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.project import Project
from specivo.models.user import User
from specivo.schemas.recurring_pattern import RecurringPatternCreate
from specivo.services.recurring_pattern_service import RecurringPatternService
from specivo.tasks import recurring
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import UserFactory

# ---------------------------------------------------------------------------
# Fixtures (mirrors test_recurring_pattern_service.py)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def project(db_session: AsyncSession) -> Project:
    proj = ProjectFactory.build(key="GEN", identifier="gen-project")
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


@pytest_asyncio.fixture
async def status_open(db_session: AsyncSession) -> IssueStatus:
    s = StatusFactory.build(name="New", position=1, category="backlog")
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


@pytest_asyncio.fixture
async def tracker(db_session: AsyncSession, status_open: IssueStatus) -> Tracker:
    t = TrackerFactory.build(name="Task", default_status_id=status_open.id)
    db_session.add(t)
    await db_session.commit()
    await db_session.refresh(t)
    return t


@pytest_asyncio.fixture
async def priority(db_session: AsyncSession) -> IssuePriority:
    p = PriorityFactory.build(name="Normal", is_default=True, position=2)
    db_session.add(p)
    await db_session.commit()
    await db_session.refresh(p)
    return p


@pytest_asyncio.fixture
async def author(db_session: AsyncSession) -> User:
    user = UserFactory.build(login="gen_author", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def service() -> RecurringPatternService:
    return RecurringPatternService()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _daily_create(
    tracker: Tracker,
    status: IssueStatus,
    priority: IssuePriority,
    *,
    dtstart: datetime,
    anchor_mode: str = "fixed",
    **overrides,
) -> RecurringPatternCreate:
    data = {
        "name": "Daily task",
        "template_tracker_id": tracker.id,
        "template_status_id": status.id,
        "template_priority_id": priority.id,
        "template_subject": "Daily task",
        "freq": "daily",
        "rrule_interval": 1,
        "dtstart": dtstart,
        "anchor_mode": anchor_mode,
        "creation_lead_time_days": 30,
    }
    data.update(overrides)
    return RecurringPatternCreate(**data)


async def _issues_for(db: AsyncSession, pattern_id: int) -> list[Issue]:
    result = await db.execute(
        select(Issue)
        .where(Issue.recurring_pattern_id == pattern_id)
        .order_by(Issue.original_occurrence_at.asc())
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Tests — drive the generation body directly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fixed_pattern_generates_in_window(
    db_session: AsyncSession,
    service: RecurringPatternService,
    project: Project,
    tracker: Tracker,
    status_open: IssueStatus,
    priority: IssuePriority,
    author: User,
) -> None:
    """A fixed pattern generates its in-window occurrences via the poller."""
    dtstart = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    now = dtstart + timedelta(days=5, hours=1)
    pattern = await service.create(
        db_session,
        project,
        _daily_create(tracker, status_open, priority, dtstart=dtstart, creation_lead_time_days=1),
        author,
    )
    await db_session.commit()

    processed, created, errors = await recurring._generate_for_session(db_session, now)

    assert processed == 1
    assert errors == 0
    assert created > 0

    issues = await _issues_for(db_session, pattern.id)
    assert len(issues) == created
    # Day-0 occurrence is present; all within the look-ahead horizon.
    horizon = now + timedelta(days=1)
    occ = sorted(i.original_occurrence_at for i in issues)
    assert occ[0] == dtstart
    assert all(dtstart <= o <= horizon for o in occ)


@pytest.mark.asyncio
async def test_flexible_open_instance_generates_nothing_more(
    db_session: AsyncSession,
    service: RecurringPatternService,
    project: Project,
    tracker: Tracker,
    status_open: IssueStatus,
    priority: IssuePriority,
    author: User,
) -> None:
    """A flexible pattern with an open latest instance generates nothing new."""
    dtstart = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    now = dtstart + timedelta(days=10, hours=1)
    pattern = await service.create(
        db_session,
        project,
        _daily_create(
            tracker, status_open, priority, dtstart=dtstart, anchor_mode="flexible", creation_lead_time_days=30
        ),
        author,
    )
    await db_session.commit()

    # First run produces exactly one (the first) occurrence and leaves it open.
    _, first_created, _ = await recurring._generate_for_session(db_session, now)
    assert first_created == 1

    # Second run: latest instance still open → nothing more.
    _, second_created, errors = await recurring._generate_for_session(db_session, now)
    assert second_created == 0
    assert errors == 0
    assert len(await _issues_for(db_session, pattern.id)) == 1


@pytest.mark.asyncio
async def test_disabled_pattern_is_skipped(
    db_session: AsyncSession,
    service: RecurringPatternService,
    project: Project,
    tracker: Tracker,
    status_open: IssueStatus,
    priority: IssuePriority,
    author: User,
) -> None:
    """A disabled pattern is never loaded and never generates issues."""
    dtstart = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    now = dtstart + timedelta(days=5, hours=1)
    pattern = await service.create(
        db_session,
        project,
        _daily_create(
            tracker, status_open, priority, dtstart=dtstart, creation_lead_time_days=1, enabled=False
        ),
        author,
    )
    await db_session.commit()

    processed, created, errors = await recurring._generate_for_session(db_session, now)

    assert processed == 0  # disabled pattern not in the enabled batch
    assert created == 0
    assert errors == 0
    assert await _issues_for(db_session, pattern.id) == []


@pytest.mark.asyncio
async def test_one_failing_pattern_does_not_block_others(
    db_session: AsyncSession,
    service: RecurringPatternService,
    project: Project,
    tracker: Tracker,
    status_open: IssueStatus,
    priority: IssuePriority,
    author: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One pattern raising during materialise leaves a healthy pattern working."""
    dtstart = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    now = dtstart + timedelta(days=5, hours=1)

    bad = await service.create(
        db_session,
        project,
        _daily_create(
            tracker, status_open, priority, dtstart=dtstart, creation_lead_time_days=1, name="Bad pattern"
        ),
        author,
    )
    good = await service.create(
        db_session,
        project,
        _daily_create(
            tracker, status_open, priority, dtstart=dtstart, creation_lead_time_days=1, name="Good pattern"
        ),
        author,
    )
    await db_session.commit()

    # Force materialize() to raise for the bad pattern only; delegate to the
    # real implementation otherwise.
    real_materialize = RecurringPatternService.materialize

    async def flaky_materialize(self, session, pattern, now, **kwargs):  # type: ignore[no-untyped-def]
        if pattern.id == bad.id:
            raise RuntimeError("boom")
        return await real_materialize(self, session, pattern, now, **kwargs)

    monkeypatch.setattr(RecurringPatternService, "materialize", flaky_materialize)

    processed, created, errors = await recurring._generate_for_session(db_session, now)

    assert processed == 2
    assert errors == 1  # the bad pattern
    assert created > 0  # the good pattern still generated

    assert await _issues_for(db_session, bad.id) == []
    assert len(await _issues_for(db_session, good.id)) == created


@pytest.mark.asyncio
async def test_second_run_is_noop(
    db_session: AsyncSession,
    service: RecurringPatternService,
    project: Project,
    tracker: Tracker,
    status_open: IssueStatus,
    priority: IssuePriority,
    author: User,
) -> None:
    """Running the generator twice creates no duplicates on the second pass."""
    dtstart = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    now = dtstart + timedelta(days=5, hours=1)
    pattern = await service.create(
        db_session,
        project,
        _daily_create(tracker, status_open, priority, dtstart=dtstart, creation_lead_time_days=1),
        author,
    )
    await db_session.commit()

    _, first_created, _ = await recurring._generate_for_session(db_session, now)
    assert first_created > 0
    count_after_first = len(await _issues_for(db_session, pattern.id))

    _, second_created, errors = await recurring._generate_for_session(db_session, now)
    assert second_created == 0
    assert errors == 0
    assert len(await _issues_for(db_session, pattern.id)) == count_after_first


@pytest.mark.asyncio
async def test_batching_processes_all_patterns(
    db_session: AsyncSession,
    service: RecurringPatternService,
    project: Project,
    tracker: Tracker,
    status_open: IssueStatus,
    priority: IssuePriority,
    author: User,
) -> None:
    """A batch_size smaller than the pattern count still processes them all."""
    dtstart = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    now = dtstart + timedelta(hours=1)
    for i in range(5):
        await service.create(
            db_session,
            project,
            _daily_create(
                tracker, status_open, priority, dtstart=dtstart, creation_lead_time_days=1, name=f"P{i}"
            ),
            author,
        )
    await db_session.commit()

    processed, created, errors = await recurring._generate_for_session(db_session, now, batch_size=2)

    assert processed == 5
    assert errors == 0
    # Each pattern: lead time 1 day → day-0 and day-1 occurrences within window.
    assert created == 10


# ---------------------------------------------------------------------------
# Full lock path — exercises the Redis distributed lock against test Redis
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_recurring_async_acquires_lock_and_generates(
    db_session: AsyncSession,
    service: RecurringPatternService,
    project: Project,
    tracker: Tracker,
    status_open: IssueStatus,
    priority: IssuePriority,
    author: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The locked entrypoint acquires the real Redis lock and runs generation.

    The test's ``db_session`` is bound to a rollback-isolated connection, so a
    session from ``task_session()`` (a fresh NullPool connection) would not see
    the uncommitted fixtures. We therefore patch ``task_session`` to reuse
    ``db_session`` via a no-op async context manager. This still exercises the
    genuine Redis distributed-lock acquisition path in
    ``_generate_recurring_async`` against the test Redis.
    """
    import uuid
    from contextlib import asynccontextmanager

    # Use a unique lock key so parallel xdist workers (sharing one test Redis)
    # never contend — we want to assert the lock is acquired, not skipped.
    monkeypatch.setattr(recurring, "_LOCK_KEY", f"specivo:test_generate_recurring:{uuid.uuid4()}")

    dtstart = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    fixed_now = dtstart + timedelta(days=3, hours=1)
    pattern = await service.create(
        db_session,
        project,
        _daily_create(tracker, status_open, priority, dtstart=dtstart, creation_lead_time_days=1),
        author,
    )
    await db_session.commit()

    # Pin "now" so generation is deterministic. The locked body imports utcnow
    # from specivo.core.utils inside the function, so patch it there.
    monkeypatch.setattr("specivo.core.utils.utcnow", lambda: fixed_now)

    @asynccontextmanager
    async def _session_cm():  # type: ignore[no-untyped-def]
        # Reuse the test session; do not close it (the fixture owns its lifecycle).
        yield db_session

    monkeypatch.setattr("specivo.tasks._async.task_session", _session_cm)

    await recurring._generate_recurring_async()

    issues = await _issues_for(db_session, pattern.id)
    assert len(issues) > 0
