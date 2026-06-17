"""Integration tests for the recurring-patterns web pages.

Covers:
- GET /projects/{key}/recurring-patterns/ renders the management list (200)
- GET /projects/{key}/recurring-patterns/{id}/ renders the detail page (200)
- detail of a nonexistent pattern -> 404
- anonymous visitor is redirected to the login page (302)

Web pages authenticate via the access_token cookie (mirroring the sprint
detail page tests). A manager who can manage recurring tasks sees the page.
"""

from __future__ import annotations

import uuid

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


async def _make_project(db: AsyncSession) -> Project:
    proj = ProjectFactory.build(key="RECW", identifier="rec-web", is_public=False)
    db.add(proj)
    await db.commit()
    await db.refresh(proj)
    db.add(EnabledModule(project_id=proj.id, name="issue_tracking"))
    await db.commit()
    await db.refresh(proj)
    return proj


async def _add_member(db: AsyncSession, project: Project, user: User, permissions: list[str]) -> None:
    role = Role(
        name=f"RecWebRole-{uuid.uuid4().hex[:8]}",
        permissions=permissions,
        builtin=0,
        assignable=True,
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
) -> tuple[Tracker, IssueStatus, IssuePriority]:
    status_open = StatusFactory.build(name="New", position=1, category="backlog")
    priority = PriorityFactory.build(name="Normal", is_default=True, position=1)
    db.add_all([status_open, priority])
    await db.commit()
    await db.refresh(status_open)
    await db.refresh(priority)
    tracker = TrackerFactory.build(name="Task", default_status_id=status_open.id)
    db.add(tracker)
    await db.commit()
    await db.refresh(tracker)
    return tracker, status_open, priority


@pytest_asyncio.fixture
async def project(db_session: AsyncSession) -> Project:
    return await _make_project(db_session)


@pytest_asyncio.fixture
async def lookups(db_session: AsyncSession) -> tuple[Tracker, IssueStatus, IssuePriority]:
    return await _make_lookups(db_session)


@pytest_asyncio.fixture
async def manager_client(
    db_session: AsyncSession,
    client: AsyncClient,
    project: Project,
) -> AsyncClient:
    user = await _make_user(db_session, login="recw_mgr")
    await _add_member(
        db_session,
        project,
        user,
        ["view_issues", "add_issues", "manage_recurring_tasks"],
    )
    token = await _login(client, user.login)
    client.headers["Authorization"] = f"Bearer {token}"
    client.cookies.set("access_token", token)
    return client


async def _create_pattern(client: AsyncClient, project_key: str, tracker_id: int) -> dict:
    resp = await client.post(
        f"/api/v1/projects/{project_key}/recurring-patterns/",
        json={
            "name": "Weekly standup notes",
            "template_tracker_id": tracker_id,
            "template_subject": "Standup notes",
            "freq": "weekly",
            "rrule_interval": 1,
            "byday": ["MO"],
            "dtstart": "2026-01-05T09:00:00+00:00",
            "timezone": "UTC",
            "creation_lead_time_days": 60,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.mark.integration
async def test_recurring_list_page_renders(
    manager_client: AsyncClient,
    project: Project,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
):
    """The management list page returns 200 and shows the created pattern."""
    tracker, _status, _priority = lookups
    await _create_pattern(manager_client, project.key, tracker.id)

    resp = await manager_client.get(f"/projects/{project.key}/recurring-patterns/")
    assert resp.status_code == 200, resp.text
    html = resp.text
    assert "Recurring tasks" in html
    assert "Weekly standup notes" in html
    # The Alpine create/edit component is wired in.
    assert "recurringPatterns(" in html


@pytest.mark.integration
async def test_recurring_detail_page_renders(
    manager_client: AsyncClient,
    project: Project,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
):
    """The pattern detail page returns 200 and shows the schedule summary."""
    tracker, _status, _priority = lookups
    pattern = await _create_pattern(manager_client, project.key, tracker.id)

    resp = await manager_client.get(f"/projects/{project.key}/recurring-patterns/{pattern['id']}/")
    assert resp.status_code == 200, resp.text
    html = resp.text
    assert "Weekly standup notes" in html
    assert "Upcoming occurrences" in html
    assert "Generated instances" in html
    # The edit-affects-future-only note is documented in the UI.
    assert "future instances only" in html
    assert "recurringPatternDetail(" in html


@pytest.mark.integration
async def test_recurring_detail_404_for_nonexistent_pattern(
    manager_client: AsyncClient,
    project: Project,
):
    """A nonexistent pattern id returns 404."""
    resp = await manager_client.get(f"/projects/{project.key}/recurring-patterns/999999/")
    assert resp.status_code == 404


@pytest.mark.integration
async def test_recurring_list_anonymous_redirects_to_login(
    client: AsyncClient,
    project: Project,
):
    """An anonymous visitor is redirected to the login page."""
    resp = await client.get(
        f"/projects/{project.key}/recurring-patterns/",
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login/"
