"""Unit / service-level tests for Sprint feature.

These tests exercise SprintService methods that do not yet exist.
They are written TDD-first and are expected to fail until the
feature is implemented.

Covered:
Sprint CRUD:
- create_sprint — creates sprint with status="planned", correct project_id
- create_sprint_with_dates — start_date and end_date stored correctly
- list_sprints_for_project — returns sprints ordered by start_date ASC
- list_sprints_empty — returns [] for project with no sprints

Sprint lifecycle:
- start_sprint — transitions planned -> active, sets start_date if None
- start_sprint_already_active_raises — ConflictError if sprint not planned
- start_second_sprint_raises — ConflictError when another sprint is active
- complete_sprint — transitions active -> completed, sets end_date if None
- complete_sprint_not_active_raises — ConflictError if sprint not active

Incomplete issue handling:
- complete_sprint_moves_incomplete_to_backlog — sprint_id=NULL on incomplete
- complete_sprint_moves_incomplete_to_next_sprint — incomplete moved to target
- complete_sprint_keeps_completed_issues — done issues stay in sprint

Velocity:
- complete_sprint_creates_velocity_snapshot — JSONB populated with counts

Delete:
- delete_sprint — deletes sprint, issues get sprint_id=NULL
- delete_active_sprint_raises — ConflictError on active sprint delete

Board data:
- board_data_groups_by_status — returns issues grouped into columns

Backlog:
- backlog_issues_returns_unassigned — only issues with sprint_id=NULL
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import ConflictError
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


async def _make_user(db: AsyncSession, login: str = "sprint_user") -> User:
    user = UserFactory.build(login=login, status="active")
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _make_project(db: AsyncSession, key: str = "SPR") -> Project:
    proj = ProjectFactory.build(key=key, identifier=f"sprint-{key.lower()}")
    db.add(proj)
    await db.commit()
    await db.refresh(proj)
    db.add(EnabledModule(project_id=proj.id, name="issue_tracking"))
    await db.commit()
    return proj


async def _make_lookups(
    db: AsyncSession,
) -> tuple[Tracker, IssueStatus, IssueStatus, IssuePriority]:
    """Create and persist a tracker, open status, closed status, and priority."""
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
    goal: str | None = None,
) -> Sprint:
    """Create a sprint through the service layer."""
    data = SprintCreate(
        name=name,
        start_date=start_date,
        end_date=end_date,
        goal=goal,
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
) -> Issue:
    """Create an issue and assign it to a sprint by setting sprint_id directly."""
    # Bump project sequence atomically
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
    return await _make_user(db_session, login="sprint_actor")


@pytest.fixture
async def lookups(
    db_session: AsyncSession,
) -> tuple[Tracker, IssueStatus, IssueStatus, IssuePriority]:
    return await _make_lookups(db_session)


# ---------------------------------------------------------------------------
# Sprint CRUD
# ---------------------------------------------------------------------------


@pytest.mark.service
async def test_create_sprint(
    db_session: AsyncSession,
    project: Project,
):
    sprint = await _create_sprint(db_session, project, "Sprint 1")

    assert sprint.name == "Sprint 1"
    assert sprint.status == "planned"
    assert sprint.project_id == project.id


@pytest.mark.service
async def test_create_sprint_with_dates(
    db_session: AsyncSession,
    project: Project,
):
    start = date(2026, 5, 1)
    end = date(2026, 5, 14)
    sprint = await _create_sprint(
        db_session,
        project,
        "Dated Sprint",
        start_date=start,
        end_date=end,
    )

    assert sprint.start_date == start
    assert sprint.end_date == end


@pytest.mark.service
async def test_list_sprints_for_project(
    db_session: AsyncSession,
    project: Project,
):
    await _create_sprint(
        db_session, project, "Sprint B", start_date=date(2026, 5, 15)
    )
    await _create_sprint(
        db_session, project, "Sprint A", start_date=date(2026, 5, 1)
    )

    sprints = await _svc.list_for_project(db_session, project.id)

    assert len(sprints) == 2
    # Ordered by start_date ASC
    assert sprints[0].name == "Sprint A"
    assert sprints[1].name == "Sprint B"


@pytest.mark.service
async def test_list_sprints_empty(
    db_session: AsyncSession,
    project: Project,
):
    sprints = await _svc.list_for_project(db_session, project.id)
    assert sprints == []


# ---------------------------------------------------------------------------
# Sprint lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.service
async def test_start_sprint(
    db_session: AsyncSession,
    project: Project,
):
    sprint = await _create_sprint(db_session, project, "To Start")
    before = datetime.now(UTC)

    started = await _svc.start_sprint(db_session, sprint)
    await db_session.commit()
    await db_session.refresh(started)

    assert started.status == "active"
    assert started.start_date is not None


@pytest.mark.service
async def test_start_sprint_already_active_raises(
    db_session: AsyncSession,
    project: Project,
):
    sprint = await _create_sprint(db_session, project, "Already Active")
    await _svc.start_sprint(db_session, sprint)
    await db_session.commit()
    await db_session.refresh(sprint)

    with pytest.raises(ConflictError):
        await _svc.start_sprint(db_session, sprint)


@pytest.mark.service
async def test_start_second_sprint_raises(
    db_session: AsyncSession,
    project: Project,
):
    sprint1 = await _create_sprint(db_session, project, "Active Sprint")
    await _svc.start_sprint(db_session, sprint1)
    await db_session.commit()

    sprint2 = await _create_sprint(db_session, project, "Second Sprint")

    with pytest.raises(ConflictError):
        await _svc.start_sprint(db_session, sprint2)


@pytest.mark.service
async def test_complete_sprint(
    db_session: AsyncSession,
    project: Project,
):
    sprint = await _create_sprint(db_session, project, "To Complete")
    await _svc.start_sprint(db_session, sprint)
    await db_session.commit()

    completed = await _svc.complete_sprint(db_session, sprint)
    await db_session.commit()
    await db_session.refresh(completed)

    assert completed.status == "completed"
    assert completed.end_date is not None


@pytest.mark.service
async def test_complete_sprint_not_active_raises(
    db_session: AsyncSession,
    project: Project,
):
    sprint = await _create_sprint(db_session, project, "Still Planned")

    with pytest.raises(ConflictError):
        await _svc.complete_sprint(db_session, sprint)


# ---------------------------------------------------------------------------
# Incomplete issue handling on sprint completion
# ---------------------------------------------------------------------------


@pytest.mark.service
async def test_complete_sprint_moves_incomplete_to_backlog(
    db_session: AsyncSession,
    project: Project,
    actor: User,
    lookups: tuple[Tracker, IssueStatus, IssueStatus, IssuePriority],
):
    tracker, status_open, status_done, priority = lookups
    sprint = await _create_sprint(db_session, project, "Backlog Sprint")
    await _svc.start_sprint(db_session, sprint)
    await db_session.commit()

    issue = await _create_issue_for_sprint(
        db_session, project, actor, sprint, "Incomplete Task",
        tracker=tracker, status=status_open, priority=priority,
    )

    await _svc.complete_sprint(db_session, sprint)
    await db_session.commit()
    await db_session.refresh(issue)

    # Incomplete issues should have sprint_id cleared (moved to backlog)
    assert issue.sprint_id is None


@pytest.mark.service
async def test_complete_sprint_moves_incomplete_to_next_sprint(
    db_session: AsyncSession,
    project: Project,
    actor: User,
    lookups: tuple[Tracker, IssueStatus, IssueStatus, IssuePriority],
):
    tracker, status_open, status_done, priority = lookups
    sprint = await _create_sprint(db_session, project, "Current Sprint")
    next_sprint = await _create_sprint(db_session, project, "Next Sprint")
    await _svc.start_sprint(db_session, sprint)
    await db_session.commit()

    issue = await _create_issue_for_sprint(
        db_session, project, actor, sprint, "Carry Over Task",
        tracker=tracker, status=status_open, priority=priority,
    )

    await _svc.complete_sprint(db_session, sprint, move_to_sprint_id=next_sprint.id)
    await db_session.commit()
    await db_session.refresh(issue)

    assert issue.sprint_id == next_sprint.id


@pytest.mark.service
async def test_complete_sprint_keeps_completed_issues(
    db_session: AsyncSession,
    project: Project,
    actor: User,
    lookups: tuple[Tracker, IssueStatus, IssueStatus, IssuePriority],
):
    tracker, status_open, status_done, priority = lookups
    sprint = await _create_sprint(db_session, project, "Done Sprint")
    await _svc.start_sprint(db_session, sprint)
    await db_session.commit()

    done_issue = await _create_issue_for_sprint(
        db_session, project, actor, sprint, "Completed Task",
        tracker=tracker, status=status_done, priority=priority,
    )

    await _svc.complete_sprint(db_session, sprint)
    await db_session.commit()
    await db_session.refresh(done_issue)

    # Done issues remain assigned to the completed sprint
    assert done_issue.sprint_id == sprint.id


# ---------------------------------------------------------------------------
# Velocity snapshot
# ---------------------------------------------------------------------------


@pytest.mark.service
async def test_complete_sprint_creates_velocity_snapshot(
    db_session: AsyncSession,
    project: Project,
    actor: User,
    lookups: tuple[Tracker, IssueStatus, IssueStatus, IssuePriority],
):
    tracker, status_open, status_done, priority = lookups
    sprint = await _create_sprint(db_session, project, "Velocity Sprint")
    await _svc.start_sprint(db_session, sprint)
    await db_session.commit()

    await _create_issue_for_sprint(
        db_session, project, actor, sprint, "Open Issue",
        tracker=tracker, status=status_open, priority=priority,
    )
    await _create_issue_for_sprint(
        db_session, project, actor, sprint, "Done Issue",
        tracker=tracker, status=status_done, priority=priority,
    )

    await _svc.complete_sprint(db_session, sprint)
    await db_session.commit()
    await db_session.refresh(sprint)

    assert sprint.velocity_snapshot is not None
    assert isinstance(sprint.velocity_snapshot, dict)
    assert "total_issues" in sprint.velocity_snapshot
    assert "completed_issues" in sprint.velocity_snapshot
    assert sprint.velocity_snapshot["total_issues"] == 2
    assert sprint.velocity_snapshot["completed_issues"] == 1


# ---------------------------------------------------------------------------
# Delete sprint
# ---------------------------------------------------------------------------


@pytest.mark.service
async def test_delete_sprint(
    db_session: AsyncSession,
    project: Project,
    actor: User,
    lookups: tuple[Tracker, IssueStatus, IssueStatus, IssuePriority],
):
    tracker, status_open, status_done, priority = lookups
    sprint = await _create_sprint(db_session, project, "Delete Me")

    issue = await _create_issue_for_sprint(
        db_session, project, actor, sprint, "Orphaned Task",
        tracker=tracker, status=status_open, priority=priority,
    )

    await _svc.delete(db_session, sprint)
    await db_session.commit()
    await db_session.refresh(issue)

    # Issues should have sprint_id set to NULL (FK SET NULL)
    assert issue.sprint_id is None


@pytest.mark.service
async def test_delete_active_sprint_raises(
    db_session: AsyncSession,
    project: Project,
):
    sprint = await _create_sprint(db_session, project, "Active No Delete")
    await _svc.start_sprint(db_session, sprint)
    await db_session.commit()

    with pytest.raises(ConflictError):
        await _svc.delete(db_session, sprint)


# ---------------------------------------------------------------------------
# Board data
# ---------------------------------------------------------------------------


@pytest.mark.service
async def test_board_data_groups_by_status(
    db_session: AsyncSession,
    project: Project,
    actor: User,
    lookups: tuple[Tracker, IssueStatus, IssueStatus, IssuePriority],
):
    tracker, status_open, status_done, priority = lookups
    sprint = await _create_sprint(db_session, project, "Board Sprint")
    await _svc.start_sprint(db_session, sprint)
    await db_session.commit()

    await _create_issue_for_sprint(
        db_session, project, actor, sprint, "Open Task",
        tracker=tracker, status=status_open, priority=priority,
    )
    await _create_issue_for_sprint(
        db_session, project, actor, sprint, "Done Task",
        tracker=tracker, status=status_done, priority=priority,
    )

    board = await _svc.board_data(db_session, sprint)

    assert isinstance(board, dict)
    # Board should contain status-name keys with lists of issues
    assert len(board) >= 2
    # Each column should be a list
    for column_issues in board.values():
        assert isinstance(column_issues, list)


# ---------------------------------------------------------------------------
# Backlog (unsprinted issues)
# ---------------------------------------------------------------------------


@pytest.mark.service
async def test_backlog_issues_returns_unassigned(
    db_session: AsyncSession,
    project: Project,
    actor: User,
    lookups: tuple[Tracker, IssueStatus, IssueStatus, IssuePriority],
):
    tracker, status_open, status_done, priority = lookups
    sprint = await _create_sprint(db_session, project, "Some Sprint")

    sprinted_issue = await _create_issue_for_sprint(
        db_session, project, actor, sprint, "Sprinted Task",
        tracker=tracker, status=status_open, priority=priority,
    )
    backlog_issue = await _create_issue_for_sprint(
        db_session, project, actor, None, "Backlog Task",
        tracker=tracker, status=status_open, priority=priority,
    )

    backlog, total = await _svc.backlog_issues(db_session, project.id)

    backlog_ids = [i.id for i in backlog]
    assert backlog_issue.id in backlog_ids
    assert sprinted_issue.id not in backlog_ids
