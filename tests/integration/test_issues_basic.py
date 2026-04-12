"""Basic integration tests for issue creation and retrieval.

(lookup models) and #63 (Issue model + IssueService).

All tests use direct service calls against a real PostgreSQL instance —
no HTTP endpoints are tested here (those come in M1.4.3).
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.project import Project
from specivo.models.user import User
from specivo.schemas.issue import IssueCreate
from specivo.services.issue_service import IssueService
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import UserFactory

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def status(db_session: AsyncSession) -> IssueStatus:
    """Persisted 'New' status."""
    s = StatusFactory.build(name="New", position=1, category="backlog")
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


@pytest_asyncio.fixture
async def closed_status(db_session: AsyncSession) -> IssueStatus:
    """Persisted 'Closed' status."""
    s = StatusFactory.build(name="Closed", position=5, category="closed")
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


@pytest_asyncio.fixture
async def tracker(db_session: AsyncSession, status: IssueStatus) -> Tracker:
    """Persisted Bug tracker with default_status_id = New."""
    t = TrackerFactory.build(name="Bug", default_status_id=status.id)
    db_session.add(t)
    await db_session.commit()
    await db_session.refresh(t)
    return t


@pytest_asyncio.fixture
async def priority(db_session: AsyncSession) -> IssuePriority:
    """Persisted Normal priority (is_default=True)."""
    result = await db_session.execute(select(IssuePriority).where(IssuePriority.name == "Normal"))
    existing = result.scalar_one_or_none()
    if existing:
        return existing
    p = PriorityFactory.build(name="Normal", is_default=True, position=2)
    db_session.add(p)
    await db_session.commit()
    await db_session.refresh(p)
    return p


@pytest_asyncio.fixture
async def project(db_session: AsyncSession) -> Project:
    """Persisted test project with key 'ACME'."""
    proj = ProjectFactory.build(key="ACME", identifier="acme-app")
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


@pytest_asyncio.fixture
async def author(db_session: AsyncSession) -> User:
    """Persisted test user."""
    user = UserFactory.build(login="issue_author", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
def service() -> IssueService:
    return IssueService()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_issue_returns_display_key(
    db_session: AsyncSession,
    project: Project,
    tracker: Tracker,
    status: IssueStatus,
    priority: IssuePriority,
    author: User,
    service: IssueService,
) -> None:
    """Created issue has correct display key format (PROJECT-N)."""
    data = IssueCreate(
        project_key=project.key,
        tracker_id=tracker.id,
        subject="First issue",
        status_id=status.id,
        priority_id=priority.id,
    )
    issue = await service.create(db_session, project, data, author)
    await db_session.commit()

    assert issue.display_key == "ACME-1"
    assert issue.project_key == "ACME"
    assert issue.sequence_number == 1


@pytest.mark.asyncio
async def test_sequence_number_increments_atomically(
    db_session: AsyncSession,
    project: Project,
    tracker: Tracker,
    status: IssueStatus,
    priority: IssuePriority,
    author: User,
    service: IssueService,
) -> None:
    """Each issue in the same project gets the next sequence number."""
    data = IssueCreate(
        project_key=project.key,
        tracker_id=tracker.id,
        subject="First issue",
        status_id=status.id,
        priority_id=priority.id,
    )

    issue1 = await service.create(db_session, project, data, author)
    await db_session.commit()

    data2 = data.model_copy(update={"subject": "Second issue"})
    issue2 = await service.create(db_session, project, data2, author)
    await db_session.commit()

    data3 = data.model_copy(update={"subject": "Third issue"})
    issue3 = await service.create(db_session, project, data3, author)
    await db_session.commit()

    assert issue1.sequence_number == 1
    assert issue2.sequence_number == 2
    assert issue3.sequence_number == 3
    assert issue1.display_key == "ACME-1"
    assert issue2.display_key == "ACME-2"
    assert issue3.display_key == "ACME-3"


@pytest.mark.asyncio
async def test_get_issue_by_display_key(
    db_session: AsyncSession,
    project: Project,
    tracker: Tracker,
    status: IssueStatus,
    priority: IssuePriority,
    author: User,
    service: IssueService,
) -> None:
    """Issue can be retrieved by its display key string."""
    data = IssueCreate(
        project_key=project.key,
        tracker_id=tracker.id,
        subject="Find me by key",
        status_id=status.id,
        priority_id=priority.id,
    )
    created = await service.create(db_session, project, data, author)
    await db_session.commit()

    fetched = await service.get_by_display_key(db_session, "ACME-1")

    assert fetched.id == created.id
    assert fetched.subject == "Find me by key"
    assert fetched.display_key == "ACME-1"


@pytest.mark.asyncio
async def test_get_issue_by_numeric_id(
    db_session: AsyncSession,
    project: Project,
    tracker: Tracker,
    status: IssueStatus,
    priority: IssuePriority,
    author: User,
    service: IssueService,
) -> None:
    """Issue can be retrieved by its internal numeric ID."""
    data = IssueCreate(
        project_key=project.key,
        tracker_id=tracker.id,
        subject="Find me by id",
        status_id=status.id,
        priority_id=priority.id,
    )
    created = await service.create(db_session, project, data, author)
    await db_session.commit()

    fetched = await service.get_by_id(db_session, created.id)

    assert fetched.id == created.id
    assert fetched.subject == "Find me by id"


@pytest.mark.asyncio
async def test_get_issue_by_display_key_using_numeric_string(
    db_session: AsyncSession,
    project: Project,
    tracker: Tracker,
    status: IssueStatus,
    priority: IssuePriority,
    author: User,
    service: IssueService,
) -> None:
    """get_by_display_key accepts bare numeric string as internal ID fallback."""
    data = IssueCreate(
        project_key=project.key,
        tracker_id=tracker.id,
        subject="Numeric string lookup",
        status_id=status.id,
        priority_id=priority.id,
    )
    created = await service.create(db_session, project, data, author)
    await db_session.commit()

    fetched = await service.get_by_display_key(db_session, str(created.id))

    assert fetched.id == created.id


@pytest.mark.asyncio
async def test_create_issue_with_default_status(
    db_session: AsyncSession,
    project: Project,
    tracker: Tracker,
    status: IssueStatus,
    priority: IssuePriority,
    author: User,
    service: IssueService,
) -> None:
    """When status_id is omitted, tracker's default_status_id is used."""
    data = IssueCreate(
        project_key=project.key,
        tracker_id=tracker.id,
        subject="Uses default status",
        priority_id=priority.id,
        # status_id intentionally omitted
    )
    issue = await service.create(db_session, project, data, author)
    await db_session.commit()

    # tracker.default_status_id points to the "New" status fixture
    assert issue.status_id == tracker.default_status_id
    assert issue.status_id == status.id


@pytest.mark.asyncio
async def test_create_issue_with_default_priority(
    db_session: AsyncSession,
    project: Project,
    tracker: Tracker,
    status: IssueStatus,
    priority: IssuePriority,
    author: User,
    service: IssueService,
) -> None:
    """When priority_id is omitted, the is_default=True priority is used."""
    data = IssueCreate(
        project_key=project.key,
        tracker_id=tracker.id,
        subject="Uses default priority",
        status_id=status.id,
        # priority_id intentionally omitted
    )
    issue = await service.create(db_session, project, data, author)
    await db_session.commit()

    # priority fixture has is_default=True
    assert issue.priority_id == priority.id


@pytest.mark.asyncio
async def test_display_key_property(
    db_session: AsyncSession,
    project: Project,
    tracker: Tracker,
    status: IssueStatus,
    priority: IssuePriority,
    author: User,
    service: IssueService,
) -> None:
    """display_key property is correctly formed from project_key and sequence_number."""
    data = IssueCreate(
        project_key=project.key,
        tracker_id=tracker.id,
        subject="Key property check",
        status_id=status.id,
        priority_id=priority.id,
    )
    issue = await service.create(db_session, project, data, author)
    await db_session.commit()

    assert issue.display_key == f"{issue.project_key}-{issue.sequence_number}"


@pytest.mark.asyncio
async def test_separate_projects_have_independent_sequences(
    db_session: AsyncSession,
    tracker: Tracker,
    status: IssueStatus,
    priority: IssuePriority,
    author: User,
    service: IssueService,
) -> None:
    """Each project has its own independent sequence counter."""
    proj_a = ProjectFactory.build(key="PRJA", identifier="project-a")
    proj_b = ProjectFactory.build(key="PRJB", identifier="project-b")
    db_session.add(proj_a)
    db_session.add(proj_b)
    await db_session.commit()
    await db_session.refresh(proj_a)
    await db_session.refresh(proj_b)

    data_a = IssueCreate(
        project_key="PRJA",
        tracker_id=tracker.id,
        subject="Issue in A",
        status_id=status.id,
        priority_id=priority.id,
    )
    data_b = IssueCreate(
        project_key="PRJB",
        tracker_id=tracker.id,
        subject="Issue in B",
        status_id=status.id,
        priority_id=priority.id,
    )

    issue_a = await service.create(db_session, proj_a, data_a, author)
    await db_session.commit()
    issue_b = await service.create(db_session, proj_b, data_b, author)
    await db_session.commit()

    assert issue_a.display_key == "PRJA-1"
    assert issue_b.display_key == "PRJB-1"
    assert issue_a.sequence_number == 1
    assert issue_b.sequence_number == 1
