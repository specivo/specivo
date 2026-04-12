"""Integration tests for Issues list filtering, sorting, and pagination.


- GET /api/v1/projects/{key}/issues with various query parameters
- status=open (default), closed, all, numeric id
- tracker_id, assigned_to_id, assigned_to_id=me, priority_id
- subject_contains (ILIKE)
- created_after / created_before / updated_after / updated_before
- sort by single and multiple fields
- pagination: offset, limit, total_count
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
from tests.factories.user import AdminUserFactory, UserFactory

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
    assigned_to_id: int | None = None,
) -> dict:
    body: dict = {
        "project_key": project_key,
        "tracker_id": tracker_id,
        "subject": subject,
        "status_id": status_id,
        "priority_id": priority_id,
    }
    if assigned_to_id is not None:
        body["assigned_to_id"] = assigned_to_id
    resp = await client.post(
        f"/api/v1/projects/{project_key}/issues/",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def open_status(db_session: AsyncSession) -> IssueStatus:
    s = StatusFactory.build(name="Open", position=1, category="backlog")
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


@pytest_asyncio.fixture
async def closed_status(db_session: AsyncSession) -> IssueStatus:
    s = StatusFactory.build(name="Closed", position=5, category="closed")
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


@pytest_asyncio.fixture
async def bug_tracker(db_session: AsyncSession, open_status: IssueStatus) -> Tracker:
    t = TrackerFactory.build(name="Bug", default_status_id=open_status.id)
    db_session.add(t)
    await db_session.commit()
    await db_session.refresh(t)
    return t


@pytest_asyncio.fixture
async def feature_tracker(db_session: AsyncSession, open_status: IssueStatus) -> Tracker:
    t = TrackerFactory.build(name="Feature", default_status_id=open_status.id)
    db_session.add(t)
    await db_session.commit()
    await db_session.refresh(t)
    return t


@pytest_asyncio.fixture
async def normal_priority(db_session: AsyncSession) -> IssuePriority:
    p = PriorityFactory.build(name="Normal", is_default=True, position=2)
    db_session.add(p)
    await db_session.commit()
    await db_session.refresh(p)
    return p


@pytest_asyncio.fixture
async def high_priority(db_session: AsyncSession) -> IssuePriority:
    p = PriorityFactory.build(name="High", is_default=False, position=3)
    db_session.add(p)
    await db_session.commit()
    await db_session.refresh(p)
    return p


@pytest_asyncio.fixture
async def project(db_session: AsyncSession) -> Project:
    proj = ProjectFactory.build(key="FILT", identifier="filter-test")
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


@pytest_asyncio.fixture
async def admin_user(db_session: AsyncSession) -> User:
    user = AdminUserFactory.build(login="filter_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def other_user(db_session: AsyncSession) -> User:
    user = UserFactory.build(login="filter_user2", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def admin_token(admin_user: User, client: AsyncClient) -> str:
    return await _login(client, admin_user.login)


# ---------------------------------------------------------------------------
# Tests: Status filtering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_issues_default_returns_open_only(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    admin_user: User,
    project: Project,
    bug_tracker: Tracker,
    open_status: IssueStatus,
    closed_status: IssueStatus,
    normal_priority: IssuePriority,
) -> None:
    await _create_issue(
        client, admin_token, project.key, bug_tracker.id, open_status.id, normal_priority.id, "Open issue"
    )
    await _create_issue(
        client, admin_token, project.key, bug_tracker.id, closed_status.id, normal_priority.id, "Closed issue"
    )

    resp = await client.get(
        f"/api/v1/projects/{project.key}/issues/",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    subjects = [i["subject"] for i in data["items"]]
    assert "Open issue" in subjects
    assert "Closed issue" not in subjects


@pytest.mark.asyncio
async def test_list_issues_status_closed(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project: Project,
    bug_tracker: Tracker,
    open_status: IssueStatus,
    closed_status: IssueStatus,
    normal_priority: IssuePriority,
) -> None:
    await _create_issue(
        client, admin_token, project.key, bug_tracker.id, open_status.id, normal_priority.id, "Open issue"
    )
    await _create_issue(
        client, admin_token, project.key, bug_tracker.id, closed_status.id, normal_priority.id, "Closed issue"
    )

    resp = await client.get(
        f"/api/v1/projects/{project.key}/issues/?status=closed",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    subjects = [i["subject"] for i in data["items"]]
    assert "Closed issue" in subjects
    assert "Open issue" not in subjects


@pytest.mark.asyncio
async def test_list_issues_status_all(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project: Project,
    bug_tracker: Tracker,
    open_status: IssueStatus,
    closed_status: IssueStatus,
    normal_priority: IssuePriority,
) -> None:
    await _create_issue(
        client, admin_token, project.key, bug_tracker.id, open_status.id, normal_priority.id, "Open issue"
    )
    await _create_issue(
        client, admin_token, project.key, bug_tracker.id, closed_status.id, normal_priority.id, "Closed issue"
    )

    resp = await client.get(
        f"/api/v1/projects/{project.key}/issues/?status=all",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total_count"] == 2


# ---------------------------------------------------------------------------
# Tests: Tracker filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_filter_by_tracker_id(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project: Project,
    bug_tracker: Tracker,
    feature_tracker: Tracker,
    open_status: IssueStatus,
    normal_priority: IssuePriority,
) -> None:
    await _create_issue(
        client, admin_token, project.key, bug_tracker.id, open_status.id, normal_priority.id, "Bug issue"
    )
    await _create_issue(
        client, admin_token, project.key, feature_tracker.id, open_status.id, normal_priority.id, "Feature issue"
    )

    resp = await client.get(
        f"/api/v1/projects/{project.key}/issues/?tracker_id={bug_tracker.id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total_count"] == 1
    assert data["items"][0]["subject"] == "Bug issue"


# ---------------------------------------------------------------------------
# Tests: assigned_to_id=me
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_filter_assigned_to_me(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    admin_user: User,
    other_user: User,
    project: Project,
    bug_tracker: Tracker,
    open_status: IssueStatus,
    normal_priority: IssuePriority,
) -> None:
    await _create_issue(
        client,
        admin_token,
        project.key,
        bug_tracker.id,
        open_status.id,
        normal_priority.id,
        "Mine",
        assigned_to_id=admin_user.id,
    )
    await _create_issue(
        client,
        admin_token,
        project.key,
        bug_tracker.id,
        open_status.id,
        normal_priority.id,
        "Others",
        assigned_to_id=other_user.id,
    )
    await _create_issue(
        client, admin_token, project.key, bug_tracker.id, open_status.id, normal_priority.id, "Unassigned"
    )

    resp = await client.get(
        f"/api/v1/projects/{project.key}/issues/?assigned_to_id=me",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    subjects = [i["subject"] for i in data["items"]]
    assert subjects == ["Mine"]


# ---------------------------------------------------------------------------
# Tests: subject_contains
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_filter_subject_contains(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project: Project,
    bug_tracker: Tracker,
    open_status: IssueStatus,
    normal_priority: IssuePriority,
) -> None:
    await _create_issue(
        client, admin_token, project.key, bug_tracker.id, open_status.id, normal_priority.id, "Fix authentication bug"
    )
    await _create_issue(
        client, admin_token, project.key, bug_tracker.id, open_status.id, normal_priority.id, "Update dashboard layout"
    )
    await _create_issue(
        client,
        admin_token,
        project.key,
        bug_tracker.id,
        open_status.id,
        normal_priority.id,
        "Authentication tests fail",
    )

    resp = await client.get(
        f"/api/v1/projects/{project.key}/issues/?subject_contains=auth&status=all",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total_count"] == 2
    subjects = {i["subject"] for i in data["items"]}
    assert "Fix authentication bug" in subjects
    assert "Authentication tests fail" in subjects
    assert "Update dashboard layout" not in subjects


# ---------------------------------------------------------------------------
# Tests: Sorting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sort_by_priority_desc(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project: Project,
    bug_tracker: Tracker,
    open_status: IssueStatus,
    normal_priority: IssuePriority,
    high_priority: IssuePriority,
) -> None:
    await _create_issue(
        client, admin_token, project.key, bug_tracker.id, open_status.id, normal_priority.id, "Normal issue"
    )
    await _create_issue(
        client, admin_token, project.key, bug_tracker.id, open_status.id, high_priority.id, "High issue"
    )

    resp = await client.get(
        f"/api/v1/projects/{project.key}/issues/?sort=priority_id:desc",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    priority_ids = [i["priority"]["id"] for i in data["items"]]
    assert priority_ids == sorted(priority_ids, reverse=True)


@pytest.mark.asyncio
async def test_multi_sort(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project: Project,
    bug_tracker: Tracker,
    open_status: IssueStatus,
    normal_priority: IssuePriority,
    high_priority: IssuePriority,
) -> None:
    await _create_issue(client, admin_token, project.key, bug_tracker.id, open_status.id, high_priority.id, "High A")
    await _create_issue(client, admin_token, project.key, bug_tracker.id, open_status.id, high_priority.id, "High B")
    await _create_issue(
        client, admin_token, project.key, bug_tracker.id, open_status.id, normal_priority.id, "Normal A"
    )

    resp = await client.get(
        f"/api/v1/projects/{project.key}/issues/?sort=priority_id:desc,created_at:asc",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total_count"] == 3
    # First two should be high priority
    assert data["items"][0]["priority"]["name"] == "High"
    assert data["items"][1]["priority"]["name"] == "High"
    assert data["items"][2]["priority"]["name"] == "Normal"


# ---------------------------------------------------------------------------
# Tests: Pagination
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pagination_offset_and_total_count(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project: Project,
    bug_tracker: Tracker,
    open_status: IssueStatus,
    normal_priority: IssuePriority,
) -> None:
    for i in range(5):
        await _create_issue(
            client, admin_token, project.key, bug_tracker.id, open_status.id, normal_priority.id, f"Issue {i}"
        )

    # First page
    resp = await client.get(
        f"/api/v1/projects/{project.key}/issues/?status=all&offset=0&limit=2",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total_count"] == 5
    assert data["offset"] == 0
    assert data["limit"] == 2
    assert len(data["items"]) == 2

    # Second page
    resp2 = await client.get(
        f"/api/v1/projects/{project.key}/issues/?status=all&offset=2&limit=2",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    data2 = resp2.json()
    assert data2["total_count"] == 5
    assert data2["offset"] == 2
    assert len(data2["items"]) == 2

    # Third page (last item)
    resp3 = await client.get(
        f"/api/v1/projects/{project.key}/issues/?status=all&offset=4&limit=2",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    data3 = resp3.json()
    assert data3["total_count"] == 5
    assert len(data3["items"]) == 1


@pytest.mark.asyncio
async def test_pagination_beyond_end_returns_empty(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project: Project,
    bug_tracker: Tracker,
    open_status: IssueStatus,
    normal_priority: IssuePriority,
) -> None:
    await _create_issue(
        client, admin_token, project.key, bug_tracker.id, open_status.id, normal_priority.id, "Only issue"
    )

    resp = await client.get(
        f"/api/v1/projects/{project.key}/issues/?status=all&offset=100&limit=25",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total_count"] == 1
    assert data["items"] == []
