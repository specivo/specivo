"""Integration tests for velocity chart and burndown data endpoints.

These tests are written TDD-first and will fail until the feature is
implemented. They drive the expected HTTP interface for burndown data
and verify the backlog page renders velocity information.

Covered:
API endpoints:
- GET /api/v1/projects/{key}/sprints/{id}/burndown/ — burndown data (200)
- GET burndown without view_issues permission             — 403
- GET burndown for nonexistent sprint                     — 404

Web pages:
- GET /projects/{key}/backlog/ — velocity table in HTML after completing a sprint
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.member import Member, MemberRole
from specivo.models.project import EnabledModule, Project
from specivo.models.role import Role
from specivo.models.user import User
from tests.factories.lookups import DoneStatusFactory, PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import TEST_PASSWORD, UserFactory

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_user(db: AsyncSession, login: str = "vb_api_user") -> User:
    user = UserFactory.build(login=login, status="active")
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _login(client: AsyncClient, login: str) -> str:
    resp = await client.post(
        "/api/v1/auth/login/",
        json={"login": login, "password": TEST_PASSWORD},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


async def _make_project(
    db: AsyncSession,
    key: str = "VBPI",
    identifier: str = "vb-api",
) -> Project:
    proj = ProjectFactory.build(key=key, identifier=identifier)
    db.add(proj)
    await db.commit()
    await db.refresh(proj)
    return proj


async def _enable_issue_tracking(db: AsyncSession, project: Project) -> None:
    db.add(EnabledModule(project_id=project.id, name="issue_tracking"))
    await db.commit()


async def _add_member_with_permissions(
    db: AsyncSession,
    project: Project,
    user: User,
    permissions: list[str],
) -> None:
    role = Role(
        name=f"TestRole-{project.key}-{user.id}",
        permissions=permissions,
        builtin=0,
    )
    db.add(role)
    await db.flush()
    member = Member(user_id=user.id, project_id=project.id)
    db.add(member)
    await db.flush()
    mr = MemberRole(member_id=member.id, role_id=role.id)
    db.add(mr)
    await db.commit()


async def _make_lookups(
    db: AsyncSession,
) -> tuple[Tracker, IssueStatus, IssueStatus, IssuePriority]:
    """Create tracker, open status, done status, and priority."""
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def project(db_session: AsyncSession) -> Project:
    proj = await _make_project(db_session)
    await _enable_issue_tracking(db_session, proj)
    return proj


@pytest_asyncio.fixture
async def vb_user(db_session: AsyncSession) -> User:
    return await _make_user(db_session, login="vb_api_mgr")


@pytest_asyncio.fixture
async def authed_client(
    db_session: AsyncSession,
    client: AsyncClient,
    project: Project,
    vb_user: User,
) -> AsyncClient:
    """Client authenticated as a manager with manage_sprints permission."""
    await _add_member_with_permissions(
        db_session,
        project,
        vb_user,
        ["view_issues", "add_issues", "manage_sprints"],
    )
    token = await _login(client, vb_user.login)
    client.headers["Authorization"] = f"Bearer {token}"
    return client


@pytest_asyncio.fixture
async def viewer_client(
    db_session: AsyncSession,
    client: AsyncClient,
    project: Project,
) -> AsyncClient:
    """Client authenticated as a view-only member (no view_issues)."""
    viewer = await _make_user(db_session, login="vb_api_noperm")
    # Deliberately grant NO permissions to test 403
    await _add_member_with_permissions(
        db_session, project, viewer, [],
    )
    token = await _login(client, viewer.login)
    client.headers["Authorization"] = f"Bearer {token}"
    return client


@pytest_asyncio.fixture
async def lookups(
    db_session: AsyncSession,
) -> tuple[Tracker, IssueStatus, IssueStatus, IssuePriority]:
    return await _make_lookups(db_session)


# ---------------------------------------------------------------------------
# Helper: create sprint and issue via API
# ---------------------------------------------------------------------------


async def _api_create_sprint(
    client: AsyncClient,
    project_key: str,
    name: str = "Sprint 1",
    **kwargs,
) -> dict:
    payload = {"name": name, **kwargs}
    resp = await client.post(
        f"/api/v1/projects/{project_key}/sprints/",
        json=payload,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _api_create_issue(
    client: AsyncClient,
    project_key: str,
    tracker_id: int,
    status_id: int,
    priority_id: int,
    subject: str,
    *,
    sprint_id: int | None = None,
    estimated_hours: float | None = None,
) -> dict:
    payload: dict = {
        "project_key": project_key,
        "tracker_id": tracker_id,
        "subject": subject,
        "status_id": status_id,
        "priority_id": priority_id,
    }
    if sprint_id is not None:
        payload["sprint_id"] = sprint_id
    if estimated_hours is not None:
        payload["estimated_hours"] = estimated_hours

    resp = await client.post(
        f"/api/v1/projects/{project_key}/issues/",
        json=payload,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Tests: Burndown API endpoint
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_api_burndown_endpoint(
    authed_client: AsyncClient,
    project: Project,
    lookups: tuple[Tracker, IssueStatus, IssueStatus, IssuePriority],
):
    """GET /api/v1/projects/{key}/sprints/{id}/burndown/ returns 200 with correct structure."""
    tracker, status_open, status_done, priority = lookups

    sprint = await _api_create_sprint(
        authed_client, project.key, "Burndown Sprint",
        start_date="2026-04-01",
        end_date="2026-04-14",
    )
    await authed_client.post(
        f"/api/v1/projects/{project.key}/sprints/{sprint['id']}/start/",
    )

    # Create issues with estimated hours
    await _api_create_issue(
        authed_client, project.key,
        tracker.id, status_done.id, priority.id,
        "Done Task",
        sprint_id=sprint["id"],
        estimated_hours=8.0,
    )
    await _api_create_issue(
        authed_client, project.key,
        tracker.id, status_open.id, priority.id,
        "Open Task",
        sprint_id=sprint["id"],
        estimated_hours=5.0,
    )

    resp = await authed_client.get(
        f"/api/v1/projects/{project.key}/sprints/{sprint['id']}/burndown/",
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert "total_estimated_hours" in data
    assert "completed_hours" in data
    assert "data_points" in data
    assert isinstance(data["data_points"], list)

    # Verify numeric values
    assert float(data["total_estimated_hours"]) == 13.0
    assert float(data["completed_hours"]) == 8.0

    # Each data point should have date, remaining_hours, ideal_remaining
    if data["data_points"]:
        point = data["data_points"][0]
        assert "date" in point
        assert "remaining_hours" in point
        assert "ideal_remaining" in point


@pytest.mark.integration
async def test_api_burndown_requires_view_permission(
    viewer_client: AsyncClient,
    project: Project,
):
    """User without view_issues gets 403 on burndown endpoint."""
    # Use an arbitrary sprint ID — permission check should happen first
    resp = await viewer_client.get(
        f"/api/v1/projects/{project.key}/sprints/99999/burndown/",
    )
    assert resp.status_code == 403


@pytest.mark.integration
async def test_api_burndown_nonexistent_sprint(
    authed_client: AsyncClient,
    project: Project,
):
    """GET burndown for nonexistent sprint returns 404."""
    resp = await authed_client.get(
        f"/api/v1/projects/{project.key}/sprints/99999/burndown/",
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tests: Backlog page shows velocity table
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_backlog_page_shows_velocity_table(
    authed_client: AsyncClient,
    db_session: AsyncSession,
    project: Project,
    lookups: tuple[Tracker, IssueStatus, IssueStatus, IssuePriority],
):
    """Complete a sprint, then verify backlog page HTML contains velocity data."""
    tracker, status_open, status_done, priority = lookups

    sprint = await _api_create_sprint(
        authed_client, project.key, "Velocity Sprint",
    )
    await authed_client.post(
        f"/api/v1/projects/{project.key}/sprints/{sprint['id']}/start/",
    )

    # Create 3 issues: 2 done, 1 open
    await _api_create_issue(
        authed_client, project.key,
        tracker.id, status_done.id, priority.id,
        "Done A",
        sprint_id=sprint["id"],
    )
    await _api_create_issue(
        authed_client, project.key,
        tracker.id, status_done.id, priority.id,
        "Done B",
        sprint_id=sprint["id"],
    )
    await _api_create_issue(
        authed_client, project.key,
        tracker.id, status_open.id, priority.id,
        "Still Open",
        sprint_id=sprint["id"],
    )

    # Complete the sprint
    resp = await authed_client.post(
        f"/api/v1/projects/{project.key}/sprints/{sprint['id']}/complete/",
    )
    assert resp.status_code == 200, resp.text

    # Get the backlog page (web endpoint, needs cookie auth)
    token = authed_client.headers["Authorization"].replace("Bearer ", "")
    page_resp = await authed_client.get(
        f"/projects/{project.key}/backlog/",
        cookies={"access_token": token},
    )
    assert page_resp.status_code == 200, page_resp.text

    html = page_resp.text
    # The backlog page should contain the completed sprint name
    assert "Velocity Sprint" in html
    # The page should display velocity data (completed issue counts)
    # Expect the sprint to show 2 completed out of 3 total
    assert "2" in html
