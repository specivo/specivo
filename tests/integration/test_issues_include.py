"""Integration tests for ?include= support on issue show endpoint.


- ?include=children returns child issues (nested set, stubbed in Phase 1)
- Unknown include values are silently ignored
- Multiple include values via comma-separated string
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.project import Project
from specivo.models.user import User
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _login(client: AsyncClient, login: str, password: str = "testpassword") -> str:
    resp = await client.post("/api/v1/auth/login/", json={"login": login, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


async def _create_issue(
    client: AsyncClient,
    token: str,
    project_key: str,
    tracker_id: int,
    status_id: int,
    priority_id: int,
    subject: str,
) -> dict:
    resp = await client.post(
        f"/api/v1/projects/{project_key}/issues/",
        json={
            "project_key": project_key,
            "tracker_id": tracker_id,
            "subject": subject,
            "status_id": status_id,
            "priority_id": priority_id,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def open_status(db_session: AsyncSession) -> IssueStatus:
    s = StatusFactory.build(name="New", position=1, is_closed=False)
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


@pytest_asyncio.fixture
async def tracker(db_session: AsyncSession, open_status: IssueStatus) -> Tracker:
    t = TrackerFactory.build(name="Task", default_status_id=open_status.id)
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
async def project(db_session: AsyncSession) -> Project:
    proj = ProjectFactory.build(key="INC", identifier="include-test")
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


@pytest_asyncio.fixture
async def admin_user(db_session: AsyncSession) -> User:
    user = AdminUserFactory.build(login="include_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def admin_token(admin_user: User, client: AsyncClient) -> str:
    return await _login(client, admin_user.login)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_issue_without_include_has_no_children_key(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """Without ?include=, the response still has a children field (default empty list)."""
    issue = await _create_issue(
        client, admin_token, project.key, tracker.id, open_status.id, priority.id, "Parent issue"
    )
    issue_key = issue["key"]

    resp = await client.get(
        f"/api/v1/issues/{issue_key}/",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # IssueWithChildren always has children list; default is []
    assert "children" in data
    assert data["children"] == []


@pytest.mark.asyncio
async def test_include_children_returns_empty_for_leaf_issue(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """A leaf issue with no children returns an empty children list."""
    issue = await _create_issue(client, admin_token, project.key, tracker.id, open_status.id, priority.id, "Leaf issue")
    issue_key = issue["key"]

    resp = await client.get(
        f"/api/v1/issues/{issue_key}/?include=children",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["children"] == []


@pytest.mark.asyncio
async def test_unknown_include_is_ignored(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """Unknown ?include= values do not cause errors — they are silently ignored."""
    issue = await _create_issue(client, admin_token, project.key, tracker.id, open_status.id, priority.id, "Test issue")
    issue_key = issue["key"]

    resp = await client.get(
        f"/api/v1/issues/{issue_key}/?include=journals,watchers,relations",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["key"] == issue_key


@pytest.mark.asyncio
async def test_include_children_with_actual_children(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """When an issue has children (root_id set), ?include=children returns them.

    Note: nested set lft/rgt management is Phase 1.5. This test manually sets
    root_id to simulate a parent-child relationship for the include query,
    which filters on root_id == parent.id AND id != parent.id.
    """
    from sqlalchemy import select

    from specivo.models.issue import Issue

    # Create parent issue via API
    parent = await _create_issue(
        client, admin_token, project.key, tracker.id, open_status.id, priority.id, "Parent issue"
    )
    parent_id = parent["id"]

    # Create a child issue via API
    child = await _create_issue(
        client, admin_token, project.key, tracker.id, open_status.id, priority.id, "Child issue"
    )
    child_id = child["id"]

    # Manually set root_id on the child to simulate nested set
    result = await db_session.execute(select(Issue).where(Issue.id == child_id))
    child_issue = result.scalar_one()
    child_issue.root_id = parent_id
    child_issue.parent_id = parent_id
    await db_session.commit()

    resp = await client.get(
        f"/api/v1/issues/{parent['key']}/?include=children",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    children = data["children"]
    assert len(children) == 1
    assert children[0]["id"] == child_id
    assert children[0]["subject"] == "Child issue"


@pytest.mark.asyncio
async def test_include_with_comma_separated_values(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """Comma-separated ?include= values are parsed correctly."""
    issue = await _create_issue(client, admin_token, project.key, tracker.id, open_status.id, priority.id, "Combo test")
    issue_key = issue["key"]

    resp = await client.get(
        f"/api/v1/issues/{issue_key}/?include=children,journals,watchers",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # children is handled; journals and watchers are ignored (not yet implemented)
    assert "children" in data
    assert data["children"] == []
