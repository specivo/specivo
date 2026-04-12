"""Integration tests for API endpoint project access control.

Verifies that API endpoints return 404 for non-member users on private projects,
covering: issues, versions, time entries, saved filters, agent sessions,
agent costs, and metadata endpoints.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.role import Role
from specivo.models.user import User
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import TEST_PASSWORD, UserFactory

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_user(db: AsyncSession, login: str) -> User:
    user = UserFactory.build(login=login, status="active")
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _make_project(db: AsyncSession, key: str, is_public: bool = False) -> Project:
    proj = ProjectFactory.build(key=key, identifier=key.lower(), is_public=is_public)
    db.add(proj)
    await db.commit()
    await db.refresh(proj)
    return proj


async def _add_member(db: AsyncSession, project: Project, user: User) -> None:
    role = Role(name=f"R-{project.key}-{user.id}", permissions=["*"], builtin=0, issues_visibility="default")
    db.add(role)
    await db.flush()
    member = Member(user_id=user.id, project_id=project.id)
    db.add(member)
    await db.flush()
    db.add(MemberRole(member_id=member.id, role_id=role.id))
    await db.commit()


async def _login(client: AsyncClient, login: str) -> str:
    resp = await client.post("/api/v1/auth/login/", json={"login": login, "password": TEST_PASSWORD})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


async def _seed_lookups(db: AsyncSession) -> tuple[Tracker, IssueStatus, IssuePriority]:
    s = StatusFactory.build(name="New", position=1, category="backlog")
    db.add(s)
    await db.flush()
    t = TrackerFactory.build(name="Bug", default_status_id=s.id)
    db.add(t)
    p = PriorityFactory.build(name="Normal", is_default=True, position=2)
    db.add(p)
    await db.commit()
    await db.refresh(s)
    await db.refresh(t)
    await db.refresh(p)
    return t, s, p


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def private_project(db_session: AsyncSession) -> Project:
    return await _make_project(db_session, "APAC", is_public=False)


@pytest_asyncio.fixture
async def outsider(db_session: AsyncSession) -> User:
    return await _make_user(db_session, "api_outsider")


@pytest_asyncio.fixture
async def member(db_session: AsyncSession, private_project: Project) -> User:
    user = await _make_user(db_session, "api_member")
    await _add_member(db_session, private_project, user)
    return user


@pytest_asyncio.fixture
async def outsider_token(client: AsyncClient, outsider: User) -> str:
    return await _login(client, "api_outsider")


@pytest_asyncio.fixture
async def member_token(client: AsyncClient, member: User) -> str:
    return await _login(client, "api_member")


# ---------------------------------------------------------------------------
# Issues API
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_issues_404_for_non_member(client: AsyncClient, private_project: Project, outsider_token: str):
    resp = await client.get(
        f"/api/v1/projects/{private_project.key}/issues/",
        headers={"Authorization": f"Bearer {outsider_token}"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_issues_ok_for_member(client: AsyncClient, private_project: Project, member_token: str):
    resp = await client.get(
        f"/api/v1/projects/{private_project.key}/issues/",
        headers={"Authorization": f"Bearer {member_token}"},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_create_issue_404_for_non_member(
    client: AsyncClient,
    db_session: AsyncSession,
    private_project: Project,
    outsider_token: str,
):
    tracker, status, priority = await _seed_lookups(db_session)
    resp = await client.post(
        f"/api/v1/projects/{private_project.key}/issues/",
        json={
            "project_key": private_project.key,
            "tracker_id": tracker.id,
            "subject": "Test",
            "status_id": status.id,
            "priority_id": priority.id,
        },
        headers={"Authorization": f"Bearer {outsider_token}"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Versions API
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_versions_404_for_non_member(client: AsyncClient, private_project: Project, outsider_token: str):
    resp = await client.get(
        f"/api/v1/projects/{private_project.key}/versions/",
        headers={"Authorization": f"Bearer {outsider_token}"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_versions_ok_for_member(client: AsyncClient, private_project: Project, member_token: str):
    resp = await client.get(
        f"/api/v1/projects/{private_project.key}/versions/",
        headers={"Authorization": f"Bearer {member_token}"},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_roadmap_404_for_non_member(client: AsyncClient, private_project: Project, outsider_token: str):
    resp = await client.get(
        f"/api/v1/projects/{private_project.key}/roadmap/",
        headers={"Authorization": f"Bearer {outsider_token}"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Time Entries API
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_time_entries_404_for_non_member(client: AsyncClient, private_project: Project, outsider_token: str):
    resp = await client.get(
        f"/api/v1/projects/{private_project.key}/time-entries/",
        headers={"Authorization": f"Bearer {outsider_token}"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_time_entries_ok_for_member(client: AsyncClient, private_project: Project, member_token: str):
    resp = await client.get(
        f"/api/v1/projects/{private_project.key}/time-entries/",
        headers={"Authorization": f"Bearer {member_token}"},
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Saved Filters API
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_saved_filters_404_for_non_member(
    client: AsyncClient, private_project: Project, outsider_token: str
):
    resp = await client.get(
        f"/api/v1/projects/{private_project.key}/saved-filters/",
        headers={"Authorization": f"Bearer {outsider_token}"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_create_saved_filter_404_for_non_member(
    client: AsyncClient, private_project: Project, outsider_token: str
):
    resp = await client.post(
        f"/api/v1/projects/{private_project.key}/saved-filters/",
        json={"name": "Test Filter", "filter_definition": {"status": "open"}},
        headers={"Authorization": f"Bearer {outsider_token}"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Metadata API
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metadata_schemas_404_for_non_member(client: AsyncClient, private_project: Project, outsider_token: str):
    resp = await client.get(
        f"/api/v1/projects/{private_project.key}/metadata-schemas/",
        headers={"Authorization": f"Bearer {outsider_token}"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Agent Costs API
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_costs_404_for_non_member(client: AsyncClient, private_project: Project, outsider_token: str):
    resp = await client.get(
        f"/api/v1/projects/{private_project.key}/agent-costs/",
        headers={"Authorization": f"Bearer {outsider_token}"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_billing_rates_404_for_non_member(client: AsyncClient, private_project: Project, outsider_token: str):
    resp = await client.get(
        f"/api/v1/projects/{private_project.key}/billing-rates/",
        headers={"Authorization": f"Bearer {outsider_token}"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Public project: non-member CAN access
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_public_project_api_accessible_to_non_member(
    client: AsyncClient, db_session: AsyncSession, outsider_token: str
):
    """Non-member can access API endpoints of a public project."""
    pub = await _make_project(db_session, "APUB", is_public=True)
    resp = await client.get(
        f"/api/v1/projects/{pub.key}/versions/",
        headers={"Authorization": f"Bearer {outsider_token}"},
    )
    assert resp.status_code == 200
