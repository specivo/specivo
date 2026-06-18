"""Integration tests for RecurringPatternService.

Covers CRUD + validation, idempotent materialisation, fixed-mode catch-up,
flexible-mode advancement (both base_date_strategy values), carry-over / reset,
assignee rotation, and the edit-scope methods (skip / override / split / plain
update). Generated issues flow through the real IssueService so we also assert
they get correct sequential display keys.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import ValidationError
from specivo.models.issue import Issue
from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.member import Member
from specivo.models.project import Project
from specivo.models.recurrence_exception import RecurrenceException
from specivo.models.user import User
from specivo.schemas.recurring_pattern import RecurringPatternCreate, RecurringPatternUpdate
from specivo.services.recurring_pattern_service import RecurringPatternService
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import UserFactory

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def project(db_session: AsyncSession) -> Project:
    proj = ProjectFactory.build(key="REC", identifier="rec-project")
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
async def status_closed(db_session: AsyncSession) -> IssueStatus:
    s = StatusFactory.build(name="Closed", position=9, category="closed")
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
    user = UserFactory.build(login="rec_author", status="active")
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


async def _make_member(db: AsyncSession, project: Project, login: str) -> User:
    user = UserFactory.build(login=login, status="active")
    db.add(user)
    await db.commit()
    await db.refresh(user)
    member = Member(project_id=project.id, user_id=user.id)
    db.add(member)
    await db.commit()
    return user


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
        "name": "Daily standup",
        "template_tracker_id": tracker.id,
        "template_status_id": status.id,
        "template_priority_id": priority.id,
        "template_subject": "Daily standup",
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
# CRUD + validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_happy_path(
    db_session: AsyncSession,
    service: RecurringPatternService,
    project: Project,
    tracker: Tracker,
    status_open: IssueStatus,
    priority: IssuePriority,
    author: User,
) -> None:
    data = _daily_create(tracker, status_open, priority, dtstart=datetime(2026, 1, 1, 9, 0, tzinfo=UTC))
    pattern = await service.create(db_session, project, data, author)
    await db_session.commit()

    assert pattern.id is not None
    assert pattern.project_id == project.id
    assert pattern.author_id == author.id
    assert pattern.freq == "daily"

    fetched = await service.get_by_id(db_session, pattern.id)
    assert fetched.id == pattern.id

    listed = await service.list_for_project(db_session, project.id)
    assert [p.id for p in listed] == [pattern.id]


@pytest.mark.asyncio
async def test_create_invalid_rule_raises(
    db_session: AsyncSession,
    service: RecurringPatternService,
    project: Project,
    tracker: Tracker,
    status_open: IssueStatus,
    priority: IssuePriority,
    author: User,
) -> None:
    """bysetpos without byday/bymonthday/bymonth is incoherent → ValidationError."""
    data = _daily_create(
        tracker,
        status_open,
        priority,
        dtstart=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
        bysetpos=[1],
    )
    with pytest.raises(ValidationError):
        await service.create(db_session, project, data, author)


@pytest.mark.asyncio
async def test_rotation_user_ids_filtered_to_members(
    db_session: AsyncSession,
    service: RecurringPatternService,
    project: Project,
    tracker: Tracker,
    status_open: IssueStatus,
    priority: IssuePriority,
    author: User,
) -> None:
    member = await _make_member(db_session, project, "rotation_member")
    non_member = UserFactory.build(login="rotation_outsider", status="active")
    db_session.add(non_member)
    await db_session.commit()
    await db_session.refresh(non_member)

    data = _daily_create(
        tracker,
        status_open,
        priority,
        dtstart=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
        assignee_rotation={"user_ids": [member.id, non_member.id], "strategy": "round_robin"},
    )
    pattern = await service.create(db_session, project, data, author)
    await db_session.commit()

    assert pattern.assignee_rotation is not None
    assert pattern.assignee_rotation["user_ids"] == [member.id]


@pytest.mark.asyncio
async def test_metadata_validation_on_create(
    db_session: AsyncSession,
    service: RecurringPatternService,
    project: Project,
    tracker: Tracker,
    status_open: IssueStatus,
    priority: IssuePriority,
    author: User,
) -> None:
    """template_metadata is validated against project/tracker schemas.

    Registers the enterprise validation feature so the (otherwise no-op)
    metadata validation actually runs, then creates a schema requiring a
    'severity' string and asserts a bad value is rejected.
    """
    from specivo.main import get_plugin_manager
    from specivo.models.metadata_schema import MetadataSchema

    get_plugin_manager().feature_registry.register("metadata_schema_validation", "test")

    schema = MetadataSchema(
        project_id=project.id,
        tracker_id=tracker.id,
        content_type="issue",
        name="severity-schema",
        schema_definition={
            "type": "object",
            "properties": {"severity": {"type": "string", "enum": ["low", "high"]}},
        },
    )
    db_session.add(schema)
    await db_session.commit()

    data = _daily_create(
        tracker,
        status_open,
        priority,
        dtstart=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
        template_metadata={"severity": "not-a-valid-value"},
    )
    with pytest.raises(ValidationError):
        await service.create(db_session, project, data, author)


# ---------------------------------------------------------------------------
# Materialisation — idempotency + fixed-mode catch-up
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_materialize_idempotent(
    db_session: AsyncSession,
    service: RecurringPatternService,
    project: Project,
    tracker: Tracker,
    status_open: IssueStatus,
    priority: IssuePriority,
    author: User,
) -> None:
    """Running materialize twice with the same `now` creates no duplicates."""
    now = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
    data = _daily_create(tracker, status_open, priority, dtstart=datetime(2026, 1, 1, 9, 0, tzinfo=UTC))
    pattern = await service.create(db_session, project, data, author)
    await db_session.commit()

    first = await service.materialize(db_session, pattern, now)
    await db_session.commit()
    assert len(first) > 0

    second = await service.materialize(db_session, pattern, now)
    await db_session.commit()
    assert second == []

    total = await _issues_for(db_session, pattern.id)
    assert len(total) == len(first)


@pytest.mark.asyncio
async def test_fixed_mode_catch_up(
    db_session: AsyncSession,
    service: RecurringPatternService,
    project: Project,
    tracker: Tracker,
    status_open: IssueStatus,
    priority: IssuePriority,
    author: User,
) -> None:
    """A daily pattern starting in the past generates the full in-window stack."""
    dtstart = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    now = dtstart + timedelta(days=10, hours=1)
    data = _daily_create(tracker, status_open, priority, dtstart=dtstart, creation_lead_time_days=30)
    pattern = await service.create(db_session, project, data, author)
    await db_session.commit()

    created = await service.materialize(db_session, pattern, now)
    await db_session.commit()

    # Window is [dtstart, now + 30d]. Daily occurrences at 09:00 — 41 days
    # of occurrences fit (day 0 through day 40 inclusive).
    occ_dates = sorted(i.original_occurrence_at for i in created)
    assert occ_dates[0] == dtstart
    # All occurrences are within the look-ahead horizon.
    horizon = now + timedelta(days=30)
    assert all(dtstart <= o <= horizon for o in occ_dates)
    # Consecutive occurrences are one day apart.
    deltas = {(b - a) for a, b in zip(occ_dates, occ_dates[1:], strict=False)}
    assert deltas == {timedelta(days=1)}


@pytest.mark.asyncio
async def test_generated_issues_get_sequential_keys(
    db_session: AsyncSession,
    service: RecurringPatternService,
    project: Project,
    tracker: Tracker,
    status_open: IssueStatus,
    priority: IssuePriority,
    author: User,
) -> None:
    """Generated issues get gap-free sequential display keys from IssueService."""
    dtstart = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    now = dtstart + timedelta(days=4, hours=1)
    data = _daily_create(tracker, status_open, priority, dtstart=dtstart, creation_lead_time_days=1)
    pattern = await service.create(db_session, project, data, author)
    await db_session.commit()

    created = await service.materialize(db_session, pattern, now)
    await db_session.commit()

    seqs = sorted(i.sequence_number for i in created)
    # Sequence numbers are contiguous (no gaps).
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))
    assert all(i.project_key == project.key for i in created)


# ---------------------------------------------------------------------------
# Carry-over + reset
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_carry_over_and_reset(
    db_session: AsyncSession,
    service: RecurringPatternService,
    project: Project,
    tracker: Tracker,
    status_open: IssueStatus,
    priority: IssuePriority,
    author: User,
) -> None:
    """Generated issue is born fresh, with template subject/description/assignee/metadata."""
    assignee = await _make_member(db_session, project, "carryover_assignee")
    dtstart = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    now = dtstart + timedelta(hours=1)
    data = _daily_create(
        tracker,
        status_open,
        priority,
        dtstart=dtstart,
        creation_lead_time_days=1,
        template_subject="Weekly report",
        template_description="Fill in the weekly numbers",
        template_assigned_to_id=assignee.id,
        template_estimated_hours=Decimal("2.5"),
        template_metadata={"checklist_done": True, "tag": "ops"},
    )
    pattern = await service.create(db_session, project, data, author)
    await db_session.commit()

    created = await service.materialize(db_session, pattern, now)
    await db_session.commit()
    assert len(created) >= 1
    # Inspect the first occurrence's generated issue.
    issue = min(created, key=lambda i: i.original_occurrence_at)

    assert issue.subject == "Weekly report"
    assert issue.description == "Fill in the weekly numbers"
    assert issue.assigned_to_id == assignee.id
    assert issue.estimated_hours == Decimal("2.5")
    # Fresh state.
    assert issue.done_ratio == 0
    assert issue.closed_on is None
    assert issue.status_id == status_open.id
    # reset_checklist flips top-level bool metadata to False; non-bool preserved.
    assert issue.issue_metadata["checklist_done"] is False
    assert issue.issue_metadata["tag"] == "ops"


# ---------------------------------------------------------------------------
# Assignee rotation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assignee_rotation_cycles(
    db_session: AsyncSession,
    service: RecurringPatternService,
    project: Project,
    tracker: Tracker,
    status_open: IssueStatus,
    priority: IssuePriority,
    author: User,
) -> None:
    """Consecutive generations cycle assignees and advance rotation_index."""
    u1 = await _make_member(db_session, project, "rot_user_1")
    u2 = await _make_member(db_session, project, "rot_user_2")

    dtstart = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    now = dtstart + timedelta(days=2, hours=1)
    data = _daily_create(
        tracker,
        status_open,
        priority,
        dtstart=dtstart,
        creation_lead_time_days=1,
        assignee_rotation={"user_ids": [u1.id, u2.id], "strategy": "round_robin"},
    )
    pattern = await service.create(db_session, project, data, author)
    await db_session.commit()

    created = await service.materialize(db_session, pattern, now)
    await db_session.commit()
    created.sort(key=lambda i: i.original_occurrence_at)

    assignees = [i.assigned_to_id for i in created]
    # Round-robin across the two members.
    assert assignees[0] == u1.id
    assert assignees[1] == u2.id
    assert assignees[2] == u1.id
    # rotation_index advanced once per generated issue.
    assert pattern.rotation_index == len(created)


# ---------------------------------------------------------------------------
# Flexible mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flexible_open_instance_blocks(
    db_session: AsyncSession,
    service: RecurringPatternService,
    project: Project,
    tracker: Tracker,
    status_open: IssueStatus,
    priority: IssuePriority,
    author: User,
) -> None:
    """Flexible mode generates nothing while the latest instance is open."""
    dtstart = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    now = dtstart + timedelta(days=10, hours=1)
    data = _daily_create(
        tracker, status_open, priority, dtstart=dtstart, anchor_mode="flexible", creation_lead_time_days=30
    )
    pattern = await service.create(db_session, project, data, author)
    await db_session.commit()

    first = await service.materialize(db_session, pattern, now)
    await db_session.commit()
    assert len(first) == 1  # only the first occurrence

    # Latest instance still open → nothing new.
    second = await service.materialize(db_session, pattern, now)
    await db_session.commit()
    assert second == []


@pytest.mark.asyncio
async def test_flexible_scheduled_strategy_advances(
    db_session: AsyncSession,
    service: RecurringPatternService,
    project: Project,
    tracker: Tracker,
    status_open: IssueStatus,
    status_closed: IssueStatus,
    priority: IssuePriority,
    author: User,
) -> None:
    """scheduled strategy: next occurrence after the latest scheduled occurrence."""
    dtstart = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    now = dtstart + timedelta(days=10, hours=1)
    data = _daily_create(
        tracker,
        status_open,
        priority,
        dtstart=dtstart,
        anchor_mode="flexible",
        base_date_strategy="scheduled",
        creation_lead_time_days=30,
    )
    pattern = await service.create(db_session, project, data, author)
    await db_session.commit()

    first = await service.materialize(db_session, pattern, now)
    await db_session.commit()
    instance = first[0]

    # Close the first instance.
    instance.status_id = status_closed.id
    instance.closed_on = now + timedelta(days=5)
    db_session.add(instance)
    await db_session.commit()

    nxt = await service.materialize(db_session, pattern, now)
    await db_session.commit()
    assert len(nxt) == 1
    # scheduled: next occurrence is one day after the first scheduled occurrence.
    assert nxt[0].original_occurrence_at == dtstart + timedelta(days=1)


@pytest.mark.asyncio
async def test_flexible_completion_strategy_advances(
    db_session: AsyncSession,
    service: RecurringPatternService,
    project: Project,
    tracker: Tracker,
    status_open: IssueStatus,
    status_closed: IssueStatus,
    priority: IssuePriority,
    author: User,
) -> None:
    """completion strategy: next occurrence anchored at the instance's closed_on."""
    dtstart = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    now = dtstart + timedelta(days=20, hours=1)
    data = _daily_create(
        tracker,
        status_open,
        priority,
        dtstart=dtstart,
        anchor_mode="flexible",
        base_date_strategy="completion",
        creation_lead_time_days=30,
    )
    pattern = await service.create(db_session, project, data, author)
    await db_session.commit()

    first = await service.materialize(db_session, pattern, now)
    await db_session.commit()
    instance = first[0]
    assert instance.original_occurrence_at == dtstart

    # Complete it late, on day 5.
    closed_on = dtstart + timedelta(days=5, hours=3)
    instance.status_id = status_closed.id
    instance.closed_on = closed_on
    db_session.add(instance)
    await db_session.commit()

    nxt = await service.materialize(db_session, pattern, now)
    await db_session.commit()
    assert len(nxt) == 1
    # completion: next scheduled occurrence strictly after closed_on (day 5 + 3h)
    # is the day-6 occurrence at 09:00.
    assert nxt[0].original_occurrence_at == dtstart + timedelta(days=6)


# ---------------------------------------------------------------------------
# Edit-scope: skip / override / split / plain update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skip_occurrence_excludes_and_deletes_untouched(
    db_session: AsyncSession,
    service: RecurringPatternService,
    project: Project,
    tracker: Tracker,
    status_open: IssueStatus,
    priority: IssuePriority,
    author: User,
) -> None:
    """skip_occurrence removes an untouched materialised issue and excludes it next run."""
    dtstart = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    now = dtstart + timedelta(days=3, hours=1)
    data = _daily_create(tracker, status_open, priority, dtstart=dtstart, creation_lead_time_days=1)
    pattern = await service.create(db_session, project, data, author)
    await db_session.commit()

    created = await service.materialize(db_session, pattern, now)
    await db_session.commit()
    target = min(i.original_occurrence_at for i in created)
    target_issue = next(i for i in created if i.original_occurrence_at == target)
    target_id = target_issue.id

    await service.skip_occurrence(db_session, pattern, target)
    await db_session.commit()

    # The untouched issue was deleted.
    assert await db_session.get(Issue, target_id) is None
    # A skip exception exists.
    result = await db_session.execute(
        select(RecurrenceException).where(
            RecurrenceException.recurring_pattern_id == pattern.id,
            RecurrenceException.kind == "skip",
        )
    )
    assert result.scalar_one_or_none() is not None

    # Re-materialising does NOT regenerate the skipped occurrence.
    again = await service.materialize(db_session, pattern, now)
    await db_session.commit()
    assert all(i.original_occurrence_at != target for i in again)


@pytest.mark.asyncio
async def test_skip_keeps_touched_issue(
    db_session: AsyncSession,
    service: RecurringPatternService,
    project: Project,
    tracker: Tracker,
    status_open: IssueStatus,
    priority: IssuePriority,
    author: User,
) -> None:
    """A touched (progressed) materialised issue is preserved on skip."""
    dtstart = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    now = dtstart + timedelta(hours=1)
    data = _daily_create(tracker, status_open, priority, dtstart=dtstart, creation_lead_time_days=1)
    pattern = await service.create(db_session, project, data, author)
    await db_session.commit()

    created = await service.materialize(db_session, pattern, now)
    await db_session.commit()
    target = created[0].original_occurrence_at
    created[0].done_ratio = 40  # touched
    db_session.add(created[0])
    await db_session.commit()
    issue_id = created[0].id

    await service.skip_occurrence(db_session, pattern, target)
    await db_session.commit()

    # The touched issue survives.
    assert await db_session.get(Issue, issue_id) is not None


@pytest.mark.asyncio
async def test_override_occurrence_applies_payload(
    db_session: AsyncSession,
    service: RecurringPatternService,
    project: Project,
    tracker: Tracker,
    status_open: IssueStatus,
    priority: IssuePriority,
    author: User,
) -> None:
    """override before materialisation applies the payload to the generated issue."""
    dtstart = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    now = dtstart + timedelta(hours=1)
    data = _daily_create(tracker, status_open, priority, dtstart=dtstart, creation_lead_time_days=1)
    pattern = await service.create(db_session, project, data, author)
    await db_session.commit()

    await service.override_occurrence(
        db_session, pattern, dtstart, {"subject": "Special edition", "description": "override body"}
    )
    await db_session.commit()

    created = await service.materialize(db_session, pattern, now)
    await db_session.commit()
    overridden = next(i for i in created if i.original_occurrence_at == dtstart)
    assert overridden.subject == "Special edition"
    assert overridden.description == "override body"

    # The override exception now links the materialised issue.
    result = await db_session.execute(
        select(RecurrenceException).where(
            RecurrenceException.recurring_pattern_id == pattern.id,
            RecurrenceException.occurrence_at == dtstart,
        )
    )
    exc = result.scalar_one()
    assert exc.materialized_issue_id == overridden.id


@pytest.mark.asyncio
async def test_override_after_materialization_updates_issue(
    db_session: AsyncSession,
    service: RecurringPatternService,
    project: Project,
    tracker: Tracker,
    status_open: IssueStatus,
    priority: IssuePriority,
    author: User,
) -> None:
    """override after materialisation updates the already-generated issue in place."""
    dtstart = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    now = dtstart + timedelta(hours=1)
    data = _daily_create(tracker, status_open, priority, dtstart=dtstart, creation_lead_time_days=1)
    pattern = await service.create(db_session, project, data, author)
    await db_session.commit()

    created = await service.materialize(db_session, pattern, now)
    await db_session.commit()
    issue_id = created[0].id

    await service.override_occurrence(db_session, pattern, dtstart, {"subject": "Patched subject"})
    await db_session.commit()

    refreshed = await db_session.get(Issue, issue_id)
    assert refreshed is not None
    assert refreshed.subject == "Patched subject"


@pytest.mark.asyncio
async def test_split_from_terminates_old_and_starts_new(
    db_session: AsyncSession,
    service: RecurringPatternService,
    project: Project,
    tracker: Tracker,
    status_open: IssueStatus,
    priority: IssuePriority,
    author: User,
) -> None:
    """split_from sets until on the old series and the new pattern starts at the boundary."""
    dtstart = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    boundary = dtstart + timedelta(days=5)
    now = dtstart + timedelta(days=10, hours=1)
    data = _daily_create(tracker, status_open, priority, dtstart=dtstart, creation_lead_time_days=30)
    pattern = await service.create(db_session, project, data, author)
    await db_session.commit()

    new_data = _daily_create(
        tracker,
        status_open,
        priority,
        dtstart=boundary,  # overwritten by split_from anyway
        creation_lead_time_days=30,
        template_subject="New series subject",
    )
    new_pattern = await service.split_from(db_session, pattern, boundary, new_data)
    await db_session.commit()

    # Old series terminated just before the boundary.
    assert pattern.until == boundary - timedelta(seconds=1)
    assert new_pattern.id != pattern.id
    assert new_pattern.dtstart == boundary

    # Old series only generates occurrences strictly before the boundary.
    old_created = await service.materialize(db_session, pattern, now)
    await db_session.commit()
    assert all(i.original_occurrence_at < boundary for i in old_created)

    # New series generates from the boundary forward.
    new_created = await service.materialize(db_session, new_pattern, now)
    await db_session.commit()
    assert min(i.original_occurrence_at for i in new_created) == boundary
    assert all(i.subject == "New series subject" for i in new_created)


@pytest.mark.asyncio
async def test_plain_update_leaves_open_instance_unchanged(
    db_session: AsyncSession,
    service: RecurringPatternService,
    project: Project,
    tracker: Tracker,
    status_open: IssueStatus,
    priority: IssuePriority,
    author: User,
) -> None:
    """A plain update changes the template only — existing open instances are untouched."""
    dtstart = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    now = dtstart + timedelta(hours=1)
    data = _daily_create(
        tracker, status_open, priority, dtstart=dtstart, creation_lead_time_days=1, template_subject="Original"
    )
    pattern = await service.create(db_session, project, data, author)
    await db_session.commit()

    created = await service.materialize(db_session, pattern, now)
    await db_session.commit()
    issue_id = created[0].id

    await service.update(
        db_session,
        pattern,
        RecurringPatternUpdate(template_subject="Renamed template", lock_version=pattern.lock_version),
    )
    await db_session.commit()

    # The already-materialised issue keeps its original subject.
    refreshed = await db_session.get(Issue, issue_id)
    assert refreshed is not None
    assert refreshed.subject == "Original"
    # But the pattern template did change.
    assert pattern.template_subject == "Renamed template"


# ---------------------------------------------------------------------------
# Template macros + provenance journal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_materialize_expands_template_macros(
    db_session: AsyncSession,
    service: RecurringPatternService,
    project: Project,
    tracker: Tracker,
    status_open: IssueStatus,
    priority: IssuePriority,
    author: User,
) -> None:
    """{{date}} macros in the subject expand per occurrence's local date."""
    dtstart = datetime(2026, 6, 18, 9, 0, tzinfo=UTC)  # a Thursday
    now = dtstart + timedelta(days=1, hours=1)
    data = _daily_create(
        tracker,
        status_open,
        priority,
        dtstart=dtstart,
        timezone="Asia/Bangkok",
        creation_lead_time_days=1,
        template_subject="Report {{day}} {{month}} {{year}}",
    )
    pattern = await service.create(db_session, project, data, author)
    await db_session.commit()

    created = await service.materialize(db_session, pattern, now)
    await db_session.commit()

    # Daily occurrences on the 18th–20th — each subject reflects its own day.
    subjects = [i.subject for i in created]
    assert subjects == [
        "Report 18 June 2026",
        "Report 19 June 2026",
        "Report 20 June 2026",
    ]


@pytest.mark.asyncio
async def test_materialize_localizes_macros(
    db_session: AsyncSession,
    service: RecurringPatternService,
    project: Project,
    tracker: Tracker,
    status_open: IssueStatus,
    priority: IssuePriority,
    author: User,
) -> None:
    """Month/weekday macros follow the supplied workspace locale (Thai here)."""
    dtstart = datetime(2026, 6, 18, 9, 0, tzinfo=UTC)
    now = dtstart + timedelta(hours=1)
    data = _daily_create(
        tracker,
        status_open,
        priority,
        dtstart=dtstart,
        timezone="Asia/Bangkok",
        creation_lead_time_days=1,
        template_subject="{{month}} {{year}}",
    )
    pattern = await service.create(db_session, project, data, author)
    await db_session.commit()

    created = await service.materialize(db_session, pattern, now, locale="th")
    await db_session.commit()

    assert created[0].subject == "มิถุนายน 2026"


@pytest.mark.asyncio
async def test_materialize_records_recurring_provenance(
    db_session: AsyncSession,
    service: RecurringPatternService,
    project: Project,
    tracker: Tracker,
    status_open: IssueStatus,
    priority: IssuePriority,
    author: User,
) -> None:
    """Each generated issue gets a 'recurring' provenance journal detail."""
    from specivo.models.journal import Journal, JournalDetail

    dtstart = datetime(2026, 6, 18, 9, 0, tzinfo=UTC)
    now = dtstart + timedelta(hours=1)
    data = _daily_create(
        tracker, status_open, priority, dtstart=dtstart, creation_lead_time_days=1
    )
    pattern = await service.create(db_session, project, data, author)
    await db_session.commit()

    created = await service.materialize(db_session, pattern, now)
    await db_session.commit()
    assert created

    # Every generated issue carries exactly one 'recurring' provenance detail
    # linking back to the pattern (id in old_value, name in new_value).
    for issue in created:
        result = await db_session.execute(
            select(JournalDetail)
            .join(Journal, JournalDetail.journal_id == Journal.id)
            .where(Journal.issue_id == issue.id, JournalDetail.property == "recurring")
        )
        details = list(result.scalars().all())
        assert len(details) == 1
        assert details[0].prop_key == "pattern"
        assert details[0].old_value == str(pattern.id)
        assert details[0].new_value == pattern.name
