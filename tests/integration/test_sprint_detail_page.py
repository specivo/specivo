"""Integration tests for the sprint detail web page.

Covers:
- GET /projects/{key}/sprints/{id}/ renders header, metric cards, and issues
- 404 when the sprint does not exist
- 404 when the sprint belongs to a different project
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
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import TEST_PASSWORD, UserFactory


async def _make_user(db: AsyncSession, login: str) -> User:
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
    key: str = "SDPG",
    identifier: str = "sprint-detail-page",
) -> Project:
    proj = ProjectFactory.build(key=key, identifier=identifier)
    db.add(proj)
    await db.commit()
    await db.refresh(proj)
    db.add(EnabledModule(project_id=proj.id, name="issue_tracking"))
    await db.commit()
    await db.refresh(proj)
    return proj


async def _add_member(
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
    db.add(MemberRole(member_id=member.id, role_id=role.id))
    await db.commit()


async def _make_lookups(
    db: AsyncSession,
) -> tuple[Tracker, IssueStatus, IssueStatus, IssuePriority]:
    status_todo = StatusFactory.build(name="New", position=1, category="backlog")
    status_done = StatusFactory.build(name="Done", position=5, category="done")
    priority = PriorityFactory.build(name="Normal", is_default=True, position=1)
    db.add_all([status_todo, status_done, priority])
    await db.commit()
    await db.refresh(status_todo)
    await db.refresh(status_done)
    await db.refresh(priority)
    tracker = TrackerFactory.build(name="Task", default_status_id=status_todo.id)
    db.add(tracker)
    await db.commit()
    await db.refresh(tracker)
    return tracker, status_todo, status_done, priority


@pytest_asyncio.fixture
async def project(db_session: AsyncSession) -> Project:
    return await _make_project(db_session)


@pytest_asyncio.fixture
async def manager_client(
    db_session: AsyncSession,
    client: AsyncClient,
    project: Project,
) -> AsyncClient:
    user = await _make_user(db_session, login="sdpg_mgr")
    await _add_member(
        db_session,
        project,
        user,
        ["view_issues", "add_issues", "manage_sprints"],
    )
    token = await _login(client, user.login)
    client.headers["Authorization"] = f"Bearer {token}"
    # Cookie auth for HTML page requests
    client.cookies.set("access_token", token)
    return client


@pytest_asyncio.fixture
async def lookups(
    db_session: AsyncSession,
) -> tuple[Tracker, IssueStatus, IssueStatus, IssuePriority]:
    return await _make_lookups(db_session)


async def _create_sprint(client: AsyncClient, project_key: str, name: str) -> dict:
    resp = await client.post(
        f"/api/v1/projects/{project_key}/sprints/",
        json={
            "name": name,
            "goal": "Ship the thing",
            "start_date": "2026-04-01",
            "end_date": "2026-04-14",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_issue(
    client: AsyncClient,
    project_key: str,
    tracker_id: int,
    status_id: int,
    priority_id: int,
    subject: str,
    sprint_id: int,
) -> dict:
    resp = await client.post(
        f"/api/v1/projects/{project_key}/issues/",
        json={
            "project_key": project_key,
            "tracker_id": tracker_id,
            "subject": subject,
            "status_id": status_id,
            "priority_id": priority_id,
            "sprint_id": sprint_id,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.mark.integration
async def test_sprint_detail_page_renders_header_metrics_and_issues(
    manager_client: AsyncClient,
    project: Project,
    lookups: tuple[Tracker, IssueStatus, IssueStatus, IssuePriority],
):
    """GET /projects/{key}/sprints/{id}/ returns 200 and renders the expected blocks."""
    tracker, status_todo, status_done, priority = lookups

    sprint = await _create_sprint(manager_client, project.key, "Sprint Detail Test")

    # 2 done, 1 open -> completion = 66%
    await _create_issue(
        manager_client, project.key,
        tracker.id, status_done.id, priority.id,
        "Shipped feature A",
        sprint_id=sprint["id"],
    )
    await _create_issue(
        manager_client, project.key,
        tracker.id, status_done.id, priority.id,
        "Shipped feature B",
        sprint_id=sprint["id"],
    )
    await _create_issue(
        manager_client, project.key,
        tracker.id, status_todo.id, priority.id,
        "Not yet started",
        sprint_id=sprint["id"],
    )

    resp = await manager_client.get(
        f"/projects/{project.key}/sprints/{sprint['id']}/",
    )
    assert resp.status_code == 200, resp.text
    html = resp.text

    # Hero header
    assert "Sprint Detail Test" in html
    assert "Ship the thing" in html
    assert "sp-sprint-hero" in html

    # Metric cards
    assert "analytics-stat-card" in html
    assert "Committed Issues" in html
    assert "Completed" in html
    assert "Completion Rate" in html
    # 3 issues committed, 2 completed, 67% completion
    assert ">3<" in html
    assert ">2<" in html
    assert "67%" in html

    # Issues list
    assert "sp-sprint-issues" in html
    assert "Shipped feature A" in html
    assert "Shipped feature B" in html
    assert "Not yet started" in html
    # Grouping labels
    assert "To Do" in html
    assert "Done" in html


@pytest.mark.integration
async def test_sprint_detail_empty_sprint_shows_empty_state(
    manager_client: AsyncClient,
    project: Project,
    lookups: tuple[Tracker, IssueStatus, IssueStatus, IssuePriority],
):
    """Sprint with no issues shows the empty-state card instead of a table."""
    sprint = await _create_sprint(manager_client, project.key, "Empty Sprint")
    resp = await manager_client.get(
        f"/projects/{project.key}/sprints/{sprint['id']}/",
    )
    assert resp.status_code == 200, resp.text
    html = resp.text
    assert "Empty Sprint" in html
    assert "No issues in this sprint yet" in html
    assert "sp-sprint-issues-table" not in html


@pytest.mark.integration
async def test_sprint_detail_404_for_nonexistent_sprint(
    manager_client: AsyncClient,
    project: Project,
):
    """Nonexistent sprint id returns 404."""
    resp = await manager_client.get(
        f"/projects/{project.key}/sprints/999999/",
    )
    assert resp.status_code == 404


@pytest.mark.integration
async def test_sprint_detail_404_when_sprint_belongs_to_other_project(
    manager_client: AsyncClient,
    db_session: AsyncSession,
    project: Project,
    lookups: tuple[Tracker, IssueStatus, IssueStatus, IssuePriority],
):
    """A sprint from another project is not reachable via this project's URL."""
    # Create a second project that the user is NOT a member of but can access
    # through another route; create sprint via direct DB to bypass permission.
    from specivo.models.sprint import Sprint

    other = await _make_project(db_session, key="OTHR", identifier="other-proj")
    sprint = Sprint(project_id=other.id, name="Foreign Sprint", status="planned")
    db_session.add(sprint)
    await db_session.commit()
    await db_session.refresh(sprint)

    resp = await manager_client.get(
        f"/projects/{project.key}/sprints/{sprint.id}/",
    )
    # Either 404 (sprint-not-in-project) or 403 (no access to other project)
    assert resp.status_code in (403, 404)
