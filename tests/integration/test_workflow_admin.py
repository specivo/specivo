"""Integration tests for admin workflow CRUD API.

Covers:
- GET /admin/workflows/transitions
- POST /admin/workflows/transitions
- DELETE /admin/workflows/transitions/{id}
- PUT /admin/workflows/transitions/bulk
- Duplicate transition -> 409
- Non-admin -> 403
- Field rule CRUD
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.lookups import IssueStatus, Tracker
from specivo.models.role import Role
from specivo.models.user import User
from tests.factories.lookups import StatusFactory, TrackerFactory
from tests.factories.user import AdminUserFactory, UserFactory

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
async def new_status(db_session: AsyncSession) -> IssueStatus:
    s = StatusFactory.build(name="New", position=1, category="backlog")
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


@pytest_asyncio.fixture
async def in_progress_status(db_session: AsyncSession) -> IssueStatus:
    s = StatusFactory.build(name="In Progress", position=2, category="backlog")
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


@pytest_asyncio.fixture
async def resolved_status(db_session: AsyncSession) -> IssueStatus:
    s = StatusFactory.build(name="Resolved", position=3, category="backlog")
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


@pytest_asyncio.fixture
async def tracker(db_session: AsyncSession, new_status: IssueStatus) -> Tracker:
    t = TrackerFactory.build(name="Bug", default_status_id=new_status.id)
    db_session.add(t)
    await db_session.commit()
    await db_session.refresh(t)
    return t


@pytest_asyncio.fixture
async def developer_role(db_session: AsyncSession) -> Role:
    result = await db_session.execute(select(Role).where(Role.name == "Developer"))
    existing = result.scalar_one_or_none()
    if existing:
        return existing
    role = Role(
        name="Developer",
        position=2,
        assignable=True,
        builtin=0,
        permissions=["add_issues", "edit_issues", "add_issue_notes", "view_issues"],
        issues_visibility="default",
        settings={},
    )
    db_session.add(role)
    await db_session.commit()
    await db_session.refresh(role)
    return role


@pytest_asyncio.fixture
async def admin_user(db_session: AsyncSession) -> User:
    user = AdminUserFactory.build(login="wf_admin2", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def regular_user(db_session: AsyncSession) -> User:
    user = UserFactory.build(login="wf_regular", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_transitions(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    tracker: Tracker,
    developer_role: Role,
    new_status: IssueStatus,
    in_progress_status: IssueStatus,
) -> None:
    """GET /admin/workflows/transitions returns list."""
    token = await _login(client, admin_user.login)

    # Create one transition first
    resp = await client.post(
        "/api/v1/admin/workflows/transitions/",
        json={
            "tracker_id": tracker.id,
            "role_id": developer_role.id,
            "old_status_id": new_status.id,
            "new_status_id": in_progress_status.id,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text

    # List
    resp = await client.get(
        "/api/v1/admin/workflows/transitions/",
        params={"tracker_id": tracker.id},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data) >= 1
    assert data[0]["tracker_id"] == tracker.id


@pytest.mark.asyncio
async def test_create_transition(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    tracker: Tracker,
    developer_role: Role,
    new_status: IssueStatus,
    in_progress_status: IssueStatus,
) -> None:
    """POST /admin/workflows/transitions -> 201."""
    token = await _login(client, admin_user.login)

    resp = await client.post(
        "/api/v1/admin/workflows/transitions/",
        json={
            "tracker_id": tracker.id,
            "role_id": developer_role.id,
            "old_status_id": new_status.id,
            "new_status_id": in_progress_status.id,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["tracker_id"] == tracker.id
    assert data["old_status_id"] == new_status.id
    assert data["new_status_id"] == in_progress_status.id
    assert "id" in data


@pytest.mark.asyncio
async def test_delete_transition(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    tracker: Tracker,
    developer_role: Role,
    new_status: IssueStatus,
    in_progress_status: IssueStatus,
) -> None:
    """DELETE /admin/workflows/transitions/{id} -> 204."""
    token = await _login(client, admin_user.login)

    # Create
    resp = await client.post(
        "/api/v1/admin/workflows/transitions/",
        json={
            "tracker_id": tracker.id,
            "role_id": developer_role.id,
            "old_status_id": new_status.id,
            "new_status_id": in_progress_status.id,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201
    tid = resp.json()["id"]

    # Delete
    resp = await client.delete(
        f"/api/v1/admin/workflows/transitions/{tid}/",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_create_duplicate_transition(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    tracker: Tracker,
    developer_role: Role,
    new_status: IssueStatus,
    in_progress_status: IssueStatus,
) -> None:
    """Attempt duplicate transition -> 409."""
    token = await _login(client, admin_user.login)
    payload = {
        "tracker_id": tracker.id,
        "role_id": developer_role.id,
        "old_status_id": new_status.id,
        "new_status_id": in_progress_status.id,
    }

    resp = await client.post(
        "/api/v1/admin/workflows/transitions/",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201

    resp = await client.post(
        "/api/v1/admin/workflows/transitions/",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_bulk_replace_transitions(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    tracker: Tracker,
    developer_role: Role,
    new_status: IssueStatus,
    in_progress_status: IssueStatus,
    resolved_status: IssueStatus,
) -> None:
    """PUT replaces all transitions for tracker+role."""
    token = await _login(client, admin_user.login)

    # Create initial transition
    await client.post(
        "/api/v1/admin/workflows/transitions/",
        json={
            "tracker_id": tracker.id,
            "role_id": developer_role.id,
            "old_status_id": new_status.id,
            "new_status_id": in_progress_status.id,
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    # Bulk replace
    resp = await client.put(
        "/api/v1/admin/workflows/transitions/bulk/",
        params={"tracker_id": tracker.id, "role_id": developer_role.id},
        json={
            "transitions": [
                {"old_status_id": new_status.id, "new_status_id": resolved_status.id},
                {"old_status_id": resolved_status.id, "new_status_id": in_progress_status.id},
            ]
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data) == 2

    # Verify old transition is gone
    resp = await client.get(
        "/api/v1/admin/workflows/transitions/",
        params={"tracker_id": tracker.id, "role_id": developer_role.id},
        headers={"Authorization": f"Bearer {token}"},
    )
    data = resp.json()
    new_status_ids = [(t["old_status_id"], t["new_status_id"]) for t in data]
    assert (new_status.id, in_progress_status.id) not in new_status_ids


@pytest.mark.asyncio
async def test_non_admin_cannot_manage_workflows(
    client: AsyncClient,
    db_session: AsyncSession,
    regular_user: User,
    tracker: Tracker,
    developer_role: Role,
    new_status: IssueStatus,
    in_progress_status: IssueStatus,
) -> None:
    """Non-admin gets 403."""
    token = await _login(client, regular_user.login)

    resp = await client.get(
        "/api/v1/admin/workflows/transitions/",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_create_field_rule(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    tracker: Tracker,
    developer_role: Role,
    in_progress_status: IssueStatus,
) -> None:
    """POST field rule -> 201."""
    token = await _login(client, admin_user.login)

    resp = await client.post(
        "/api/v1/admin/workflows/field-rules/",
        json={
            "tracker_id": tracker.id,
            "role_id": developer_role.id,
            "status_id": in_progress_status.id,
            "field_name": "assigned_to_id",
            "rule": "required",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["field_name"] == "assigned_to_id"
    assert data["rule"] == "required"


@pytest.mark.asyncio
async def test_delete_field_rule(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_user: User,
    tracker: Tracker,
    developer_role: Role,
    in_progress_status: IssueStatus,
) -> None:
    """DELETE field rule -> 204."""
    token = await _login(client, admin_user.login)

    # Create
    resp = await client.post(
        "/api/v1/admin/workflows/field-rules/",
        json={
            "tracker_id": tracker.id,
            "role_id": developer_role.id,
            "status_id": in_progress_status.id,
            "field_name": "assigned_to_id",
            "rule": "required",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201
    rid = resp.json()["id"]

    # Delete
    resp = await client.delete(
        f"/api/v1/admin/workflows/field-rules/{rid}/",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 204
