"""Integration tests for reaction toggle API endpoint."""

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


async def _create_issue_via_api(
    client: AsyncClient,
    token: str,
    project_key: str,
    tracker_id: int,
    status_id: int,
    priority_id: int,
    subject: str,
) -> dict:
    payload = {
        "project_key": project_key,
        "tracker_id": tracker_id,
        "subject": subject,
        "status_id": status_id,
        "priority_id": priority_id,
    }
    resp = await client.post(
        f"/api/v1/projects/{project_key}/issues/",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def open_status(db_session: AsyncSession) -> IssueStatus:
    s = StatusFactory.build(name="New", position=1, category="backlog")
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
    proj = ProjectFactory.build(key="RXN", identifier="reaction-test")
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


@pytest_asyncio.fixture
async def admin_user(db_session: AsyncSession) -> User:
    user = AdminUserFactory.build(login="reaction_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def admin_token(admin_user: User, client: AsyncClient) -> str:
    return await _login(client, admin_user.login)


@pytest_asyncio.fixture
async def issue_key(
    client: AsyncClient,
    admin_token: str,
    project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> str:
    data = await _create_issue_via_api(
        client, admin_token, project.key, tracker.id, open_status.id, priority.id, "Reaction test issue"
    )
    return data["key"]


@pytest_asyncio.fixture
async def journal_id(client: AsyncClient, admin_token: str, issue_key: str) -> int:
    resp = await client.post(
        f"/api/v1/issues/{issue_key}/journals/",
        json={"notes": "Test comment for reactions"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_toggle_reaction_add(
    client: AsyncClient,
    admin_token: str,
    issue_key: str,
    journal_id: int,
) -> None:
    """POST adds a reaction and returns added=true."""
    resp = await client.post(
        f"/api/v1/issues/{issue_key}/journals/{journal_id}/reactions/thumbs_up/",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["added"] is True
    assert data["emoji"] == "thumbs_up"


@pytest.mark.integration
async def test_toggle_reaction_remove(
    client: AsyncClient,
    admin_token: str,
    issue_key: str,
    journal_id: int,
) -> None:
    """POST twice removes the reaction and returns added=false."""
    url = f"/api/v1/issues/{issue_key}/journals/{journal_id}/reactions/thumbs_up/"
    headers = {"Authorization": f"Bearer {admin_token}"}

    # First toggle — add
    resp = await client.post(url, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["added"] is True

    # Second toggle — remove
    resp = await client.post(url, headers=headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["added"] is False
    assert data["emoji"] == "thumbs_up"


@pytest.mark.integration
async def test_invalid_emoji_rejected(
    client: AsyncClient,
    admin_token: str,
    issue_key: str,
    journal_id: int,
) -> None:
    """Invalid emoji returns 422 (validation error from service)."""
    resp = await client.post(
        f"/api/v1/issues/{issue_key}/journals/{journal_id}/reactions/not_a_real_emoji/",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.integration
async def test_reaction_requires_auth(
    client: AsyncClient,
    issue_key: str,
    journal_id: int,
) -> None:
    """Unauthenticated request returns 401."""
    resp = await client.post(
        f"/api/v1/issues/{issue_key}/journals/{journal_id}/reactions/thumbs_up/",
    )
    assert resp.status_code == 401, resp.text
