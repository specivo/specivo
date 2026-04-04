"""Integration tests for Issues CRUD API.

Covers:
- POST /api/v1/projects/{key}/issues  — create
- GET  /api/v1/issues/{ref}           — show by display key
- GET  /api/v1/issues/{numeric_id}    — show by numeric ID
- PATCH /api/v1/issues/{ref}          — update with lock_version
- DELETE /api/v1/issues/{ref}         — delete
- Optimistic locking: stale lock_version → 409
- 409 body includes current lock_version in details
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
async def closed_status(db_session: AsyncSession) -> IssueStatus:
    s = StatusFactory.build(name="Closed", position=5, is_closed=True)
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


@pytest_asyncio.fixture
async def tracker(db_session: AsyncSession, open_status: IssueStatus) -> Tracker:
    t = TrackerFactory.build(name="Bug", default_status_id=open_status.id)
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
    proj = ProjectFactory.build(key="ACME", identifier="acme-app")
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


@pytest_asyncio.fixture
async def admin_user(db_session: AsyncSession) -> User:
    user = AdminUserFactory.build(login="crud_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def admin_token(admin_user: User, client: AsyncClient) -> str:
    return await _login(client, admin_user.login)


# ---------------------------------------------------------------------------
# Tests: Create
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_issue_returns_display_key(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    resp = await client.post(
        f"/api/v1/projects/{project.key}/issues/",
        json={
            "project_key": project.key,
            "tracker_id": tracker.id,
            "subject": "First issue",
            "status_id": open_status.id,
            "priority_id": priority.id,
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["key"] == "ACME-1"
    assert data["project_key"] == "ACME"
    assert data["subject"] == "First issue"
    assert data["tracker"]["name"] == "Bug"
    assert data["status"]["name"] == "New"
    assert data["priority"]["name"] == "Normal"
    assert data["lock_version"] >= 0  # may be 0 or 1 depending on SQLAlchemy version_id_col behavior
    assert "id" in data
    assert "created_at" in data


# ---------------------------------------------------------------------------
# Tests: Get by display key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_issue_by_display_key(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    create_resp = await client.post(
        f"/api/v1/projects/{project.key}/issues/",
        json={
            "project_key": project.key,
            "tracker_id": tracker.id,
            "subject": "Find me",
            "status_id": open_status.id,
            "priority_id": priority.id,
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert create_resp.status_code == 201

    resp = await client.get(
        "/api/v1/issues/ACME-1/",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["key"] == "ACME-1"
    assert data["subject"] == "Find me"


@pytest.mark.asyncio
async def test_get_issue_by_numeric_id(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    create_resp = await client.post(
        f"/api/v1/projects/{project.key}/issues/",
        json={
            "project_key": project.key,
            "tracker_id": tracker.id,
            "subject": "Numeric lookup",
            "status_id": open_status.id,
            "priority_id": priority.id,
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert create_resp.status_code == 201
    issue_id = create_resp.json()["id"]

    resp = await client.get(
        f"/api/v1/issues/{issue_id}/",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == issue_id
    assert resp.json()["subject"] == "Numeric lookup"


@pytest.mark.asyncio
async def test_get_issue_not_found(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    resp = await client.get(
        "/api/v1/issues/ACME-999/",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tests: Update (PATCH)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_issue(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    closed_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    create_resp = await client.post(
        f"/api/v1/projects/{project.key}/issues/",
        json={
            "project_key": project.key,
            "tracker_id": tracker.id,
            "subject": "Update me",
            "status_id": open_status.id,
            "priority_id": priority.id,
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert create_resp.status_code == 201
    issue_key = create_resp.json()["key"]
    lock_version = create_resp.json()["lock_version"]

    resp = await client.patch(
        f"/api/v1/issues/{issue_key}/",
        json={
            "subject": "Updated subject",
            "status_id": closed_status.id,
            "lock_version": lock_version,
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["subject"] == "Updated subject"
    assert data["status"]["id"] == closed_status.id
    # lock_version must have incremented
    assert data["lock_version"] == lock_version + 1


# ---------------------------------------------------------------------------
# Tests: Delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_issue(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    create_resp = await client.post(
        f"/api/v1/projects/{project.key}/issues/",
        json={
            "project_key": project.key,
            "tracker_id": tracker.id,
            "subject": "Delete me",
            "status_id": open_status.id,
            "priority_id": priority.id,
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert create_resp.status_code == 201
    issue_key = create_resp.json()["key"]

    del_resp = await client.delete(
        f"/api/v1/issues/{issue_key}/",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert del_resp.status_code == 204

    get_resp = await client.get(
        f"/api/v1/issues/{issue_key}/",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert get_resp.status_code == 404


# ---------------------------------------------------------------------------
# Tests: Optimistic locking
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_with_stale_lock_version_returns_409(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    create_resp = await client.post(
        f"/api/v1/projects/{project.key}/issues/",
        json={
            "project_key": project.key,
            "tracker_id": tracker.id,
            "subject": "Lock version test",
            "status_id": open_status.id,
            "priority_id": priority.id,
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert create_resp.status_code == 201
    issue_key = create_resp.json()["key"]
    current_lock_version = create_resp.json()["lock_version"]  # 0

    # Submit a stale lock_version (negative or wrong value)
    resp = await client.patch(
        f"/api/v1/issues/{issue_key}/",
        json={
            "subject": "Conflicting update",
            "lock_version": current_lock_version + 99,  # stale
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 409, resp.text
    errors = resp.json()["errors"]
    assert len(errors) > 0
    assert errors[0]["code"] == "conflict_lock_version"


@pytest.mark.asyncio
async def test_409_includes_current_lock_version(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    create_resp = await client.post(
        f"/api/v1/projects/{project.key}/issues/",
        json={
            "project_key": project.key,
            "tracker_id": tracker.id,
            "subject": "Lock details test",
            "status_id": open_status.id,
            "priority_id": priority.id,
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert create_resp.status_code == 201
    issue_key = create_resp.json()["key"]
    current_lock_version = create_resp.json()["lock_version"]  # 0

    resp = await client.patch(
        f"/api/v1/issues/{issue_key}/",
        json={
            "subject": "Another conflict",
            "lock_version": 999,  # wrong
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 409, resp.text
    errors = resp.json()["errors"]
    assert errors[0]["details"]["current_lock_version"] == current_lock_version
