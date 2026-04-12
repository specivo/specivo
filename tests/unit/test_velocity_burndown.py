"""Unit / service-level tests for velocity chart and burndown data.

These tests exercise SprintService methods that do not yet exist.
They are written TDD-first and are expected to fail until the
feature is implemented.

Covered:
Velocity:
- velocity_summary_for_completed_sprint — snapshot has correct counts
  (already tested in test_sprint_service.py — skipped here)
- velocity_summary_empty_sprint — 0/0 snapshot for sprint with no issues
- velocity_across_sprints — average velocity across 3 completed sprints

Burndown:
- burndown_data_active_sprint — returns total/completed estimated hours
- burndown_data_no_estimates — returns 0 totals when no estimated_hours set
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.issue import Issue
from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.project import EnabledModule, Project
from specivo.models.sprint import Sprint
from specivo.models.user import User
from specivo.schemas.sprint import SprintCreate
from specivo.services.sprint_service import SprintService
from tests.factories.lookups import (
    DoneStatusFactory,
    PriorityFactory,
    StatusFactory,
    TrackerFactory,
)
from tests.factories.project import ProjectFactory
from tests.factories.user import UserFactory

# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_svc = SprintService()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_user(db: AsyncSession, login: str = "vb_user") -> User:
    user = UserFactory.build(login=login, status="active")
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _make_project(db: AsyncSession, key: str = "VBD") -> Project:
    proj = ProjectFactory.build(key=key, identifier=f"vb-{key.lower()}")
    db.add(proj)
    await db.commit()
    await db.refresh(proj)
    db.add(EnabledModule(project_id=proj.id, name="issue_tracking"))
    await db.commit()
    return proj


async def _make_lookups(
    db: AsyncSession,
) -> tuple[Tracker, IssueStatus, IssueStatus, IssuePriority]:
    """Create and persist a tracker, open status, done status, and priority."""
    status_open = StatusFactory.build(name="New", position=1, category="backlog")
    status_done = DoneStatusFactory.build(name="Done", position=5, category="done")
    priority = PriorityFactory.build(name="Normal", is_default=True, position=1)

    db.add_all([status_open, status_done, priority])
    await db.commit()
    await db.refresh(status_open)
    await db.refresh(status_done)
    await db.refresh(priority)

    tracker = TrackerFactory.build(name="Task", default_status_id=status_open.id)
    db.add(tracker)
    await db.commit()
    await db.refresh(tracker)

    return tracker, status_open, status_done, priority


async def _create_sprint(
    db: AsyncSession,
    project: Project,
    name: str = "Sprint 1",
    *,
    start_date: date | None = None,
    end_date: date | None = None,
) -> Sprint:
    """Create a sprint through the service layer."""
    data = SprintCreate(
        name=name,
        start_date=start_date,
        end_date=end_date,
    )
    sprint = await _svc.create(db, project, data)
    await db.commit()
    await db.refresh(sprint)
    return sprint


async def _create_issue_for_sprint(
    db: AsyncSession,
    project: Project,
    user: User,
    sprint: Sprint | None,
    subject: str,
    *,
    tracker: Tracker,
    status: IssueStatus,
    priority: IssuePriority,
    estimated_hours: Decimal | None = None,
) -> Issue:
    """Create an issue and assign it to a sprint by setting sprint_id directly."""
    project.issue_sequence += 1
    seq = project.issue_sequence

    issue = Issue(
        project_id=project.id,
        project_key=project.key,
        sequence_number=seq,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=user.id,
        subject=subject,
        sprint_id=sprint.id if sprint else None,
        estimated_hours=estimated_hours,
    )
    db.add(issue)
    await db.commit()
    await db.refresh(issue)
    return issue


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def project(db_session: AsyncSession) -> Project:
    return await _make_project(db_session)


@pytest.fixture
async def actor(db_session: AsyncSession) -> User:
    return await _make_user(db_session, login="vb_actor")


@pytest.fixture
async def lookups(
    db_session: AsyncSession,
) -> tuple[Tracker, IssueStatus, IssueStatus, IssuePriority]:
    return await _make_lookups(db_session)


# ---------------------------------------------------------------------------
# Velocity: empty sprint
# ---------------------------------------------------------------------------


@pytest.mark.service
async def test_velocity_summary_empty_sprint(
    db_session: AsyncSession,
    project: Project,
):
    """Complete a sprint with zero issues. Snapshot should have 0/0."""
    sprint = await _create_sprint(db_session, project, "Empty Sprint")
    await _svc.start_sprint(db_session, sprint)
    await db_session.commit()

    await _svc.complete_sprint(db_session, sprint)
    await db_session.commit()
    await db_session.refresh(sprint)

    assert sprint.velocity_snapshot is not None
    assert sprint.velocity_snapshot["total_issues"] == 0
    assert sprint.velocity_snapshot["completed_issues"] == 0


# ---------------------------------------------------------------------------
# Velocity: average across multiple sprints
# ---------------------------------------------------------------------------


@pytest.mark.service
async def test_velocity_across_sprints(
    db_session: AsyncSession,
    project: Project,
    actor: User,
    lookups: tuple[Tracker, IssueStatus, IssueStatus, IssuePriority],
):
    """Complete 3 sprints with different issue counts. Verify average velocity."""
    tracker, status_open, status_done, priority = lookups

    # Sprint 1: 2 done out of 3
    s1 = await _create_sprint(db_session, project, "Velocity S1")
    await _svc.start_sprint(db_session, s1)
    await db_session.commit()
    for i in range(2):
        await _create_issue_for_sprint(
            db_session, project, actor, s1, f"S1 Done {i}",
            tracker=tracker, status=status_done, priority=priority,
        )
    await _create_issue_for_sprint(
        db_session, project, actor, s1, "S1 Open",
        tracker=tracker, status=status_open, priority=priority,
    )
    await _svc.complete_sprint(db_session, s1)
    await db_session.commit()

    # Sprint 2: 4 done out of 4
    s2 = await _create_sprint(db_session, project, "Velocity S2")
    await _svc.start_sprint(db_session, s2)
    await db_session.commit()
    for i in range(4):
        await _create_issue_for_sprint(
            db_session, project, actor, s2, f"S2 Done {i}",
            tracker=tracker, status=status_done, priority=priority,
        )
    await _svc.complete_sprint(db_session, s2)
    await db_session.commit()

    # Sprint 3: 3 done out of 5
    s3 = await _create_sprint(db_session, project, "Velocity S3")
    await _svc.start_sprint(db_session, s3)
    await db_session.commit()
    for i in range(3):
        await _create_issue_for_sprint(
            db_session, project, actor, s3, f"S3 Done {i}",
            tracker=tracker, status=status_done, priority=priority,
        )
    for i in range(2):
        await _create_issue_for_sprint(
            db_session, project, actor, s3, f"S3 Open {i}",
            tracker=tracker, status=status_open, priority=priority,
        )
    await _svc.complete_sprint(db_session, s3)
    await db_session.commit()

    # Call average_velocity — new method that should exist on SprintService
    avg = await _svc.average_velocity(db_session, project.id)

    # Average completed: (2 + 4 + 3) / 3 = 3.0
    assert avg == Decimal("3")


# ---------------------------------------------------------------------------
# Burndown: active sprint with estimates
# ---------------------------------------------------------------------------


@pytest.mark.service
async def test_burndown_data_active_sprint(
    db_session: AsyncSession,
    project: Project,
    actor: User,
    lookups: tuple[Tracker, IssueStatus, IssueStatus, IssuePriority],
):
    """Active sprint with estimated_hours on issues. Verify burndown structure."""
    tracker, status_open, status_done, priority = lookups

    sprint = await _create_sprint(
        db_session, project, "Burndown Sprint",
        start_date=date(2026, 4, 1),
        end_date=date(2026, 4, 14),
    )
    await _svc.start_sprint(db_session, sprint)
    await db_session.commit()

    # Issue 1: 8h estimated, done
    await _create_issue_for_sprint(
        db_session, project, actor, sprint, "Task A",
        tracker=tracker, status=status_done, priority=priority,
        estimated_hours=Decimal("8"),
    )
    # Issue 2: 5h estimated, open
    await _create_issue_for_sprint(
        db_session, project, actor, sprint, "Task B",
        tracker=tracker, status=status_open, priority=priority,
        estimated_hours=Decimal("5"),
    )
    # Issue 3: 3h estimated, open
    await _create_issue_for_sprint(
        db_session, project, actor, sprint, "Task C",
        tracker=tracker, status=status_open, priority=priority,
        estimated_hours=Decimal("3"),
    )

    result = await _svc.burndown_data(db_session, sprint)

    assert isinstance(result, dict)
    assert result["total_estimated_hours"] == Decimal("16")
    assert result["completed_hours"] == Decimal("8")
    assert isinstance(result["data_points"], list)
    assert len(result["data_points"]) > 0

    # Each data point should have date, remaining, and ideal keys
    point = result["data_points"][0]
    assert "date" in point
    assert "remaining" in point
    assert "ideal" in point


# ---------------------------------------------------------------------------
# Burndown: no estimates
# ---------------------------------------------------------------------------


@pytest.mark.service
async def test_burndown_data_no_estimates(
    db_session: AsyncSession,
    project: Project,
    actor: User,
    lookups: tuple[Tracker, IssueStatus, IssueStatus, IssuePriority],
):
    """Active sprint where issues have no estimated_hours. Totals should be 0."""
    tracker, status_open, status_done, priority = lookups

    sprint = await _create_sprint(
        db_session, project, "No Estimates Sprint",
        start_date=date(2026, 4, 1),
        end_date=date(2026, 4, 14),
    )
    await _svc.start_sprint(db_session, sprint)
    await db_session.commit()

    await _create_issue_for_sprint(
        db_session, project, actor, sprint, "No Est Task",
        tracker=tracker, status=status_open, priority=priority,
    )

    result = await _svc.burndown_data(db_session, sprint)

    assert result["total_estimated_hours"] == Decimal("0")
    assert result["completed_hours"] == Decimal("0")
    assert isinstance(result["data_points"], list)
