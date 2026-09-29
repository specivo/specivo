"""Integration tests: deleting a relation requires seeing both issues.

``DELETE /api/v1/relations/{id}/`` mirrors relation creation. The caller must
be able to see both linked issues and must hold ``manage_issue_relations`` on
at least one of their projects. When the other issue is invisible the
relation is answered with 404, exactly like a relation that does not exist,
and no journal entry is written on either issue.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.journal import Journal
from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.relation import IssueRelation
from specivo.models.role import Role
from specivo.models.user import User
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, UserFactory

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _login(client: AsyncClient, login: str, password: str = "testpassword") -> str:
    resp = await client.post("/api/v1/auth/login/", json={"login": login, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _create_issue(
    client: AsyncClient,
    token: str,
    project: Project,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
    subject: str,
    *,
    is_private: bool = False,
) -> dict:
    tracker, status, priority = lookups
    resp = await client.post(
        f"/api/v1/projects/{project.key}/issues/",
        json={
            "project_key": project.key,
            "tracker_id": tracker.id,
            "status_id": status.id,
            "priority_id": priority.id,
            "subject": subject,
            "is_private": is_private,
        },
        headers=_auth(token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_relation(client: AsyncClient, token: str, issue_ref: str, issue_to_key: str) -> int:
    resp = await client.post(
        f"/api/v1/issues/{issue_ref}/relations/",
        json={"issue_to_key": issue_to_key, "relation_type": "relates"},
        headers=_auth(token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _make_role(db: AsyncSession, permissions: list[str]) -> Role:
    role = Role(
        name=f"RelDel-{uuid.uuid4().hex[:8]}",
        position=5,
        assignable=True,
        builtin=0,
        permissions=permissions,
        issues_visibility="default",
        settings={},
    )
    db.add(role)
    await db.commit()
    await db.refresh(role)
    return role


async def _grant(db: AsyncSession, project: Project, user: User, role: Role) -> None:
    member = Member(project_id=project.id, user_id=user.id)
    db.add(member)
    await db.flush()
    db.add(MemberRole(member_id=member.id, role_id=role.id))
    await db.commit()


async def _journal_count(db: AsyncSession, issue_id: int) -> int:
    result = await db.execute(select(func.count()).select_from(Journal).where(Journal.issue_id == issue_id))
    return result.scalar_one()


async def _relation_exists(db: AsyncSession, relation_id: int) -> bool:
    result = await db.execute(select(IssueRelation.id).where(IssueRelation.id == relation_id))
    return result.scalar_one_or_none() is not None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def lookups(db_session: AsyncSession) -> tuple[Tracker, IssueStatus, IssuePriority]:
    status = StatusFactory.build(name="New", position=1, category="backlog")
    db_session.add(status)
    await db_session.commit()
    await db_session.refresh(status)
    tracker = TrackerFactory.build(name="Task", default_status_id=status.id)
    priority = PriorityFactory.build(name="Normal", is_default=True, position=2)
    db_session.add_all([tracker, priority])
    await db_session.commit()
    await db_session.refresh(tracker)
    await db_session.refresh(priority)
    return tracker, status, priority


@pytest_asyncio.fixture
async def admin_token(db_session: AsyncSession, client: AsyncClient) -> str:
    admin = AdminUserFactory.build(login="rdv_admin", status="active")
    db_session.add(admin)
    await db_session.commit()
    return await _login(client, admin.login)


@pytest_asyncio.fixture
async def member_user(db_session: AsyncSession) -> User:
    user = UserFactory.build(login="rdv_member", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def member_token(member_user: User, client: AsyncClient) -> str:
    return await _login(client, member_user.login)


@pytest_asyncio.fixture
async def project_a(db_session: AsyncSession) -> Project:
    proj = ProjectFactory.build(key="RDVA", identifier="rdv-project-a", is_public=False)
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


@pytest_asyncio.fixture
async def public_project_b(db_session: AsyncSession) -> Project:
    proj = ProjectFactory.build(key="RDVB", identifier="rdv-project-b", is_public=True)
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


@pytest_asyncio.fixture
async def private_project_c(db_session: AsyncSession) -> Project:
    proj = ProjectFactory.build(key="RDVC", identifier="rdv-project-c", is_public=False)
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


@pytest_asyncio.fixture
async def relations_role(db_session: AsyncSession) -> Role:
    return await _make_role(db_session, ["view_issues", "edit_issues", "manage_issue_relations"])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_delete_refused_when_other_issue_is_private(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    member_user: User,
    member_token: str,
    project_a: Project,
    public_project_b: Project,
    relations_role: Role,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
) -> None:
    """A private issue in another project stays hidden: 404, relation kept, no journals."""
    issue_a = await _create_issue(client, admin_token, project_a, lookups, "Visible task")
    issue_b = await _create_issue(client, admin_token, public_project_b, lookups, "Hidden task", is_private=True)
    relation_id = await _create_relation(client, admin_token, issue_a["key"], issue_b["key"])
    await _grant(db_session, project_a, member_user, relations_role)

    journals_a = await _journal_count(db_session, issue_a["id"])
    journals_b = await _journal_count(db_session, issue_b["id"])

    resp = await client.delete(f"/api/v1/relations/{relation_id}/", headers=_auth(member_token))

    assert resp.status_code == 404, resp.text
    assert issue_b["key"] not in resp.text
    assert await _relation_exists(db_session, relation_id)
    assert await _journal_count(db_session, issue_a["id"]) == journals_a
    assert await _journal_count(db_session, issue_b["id"]) == journals_b


async def test_delete_refused_when_other_project_is_not_joined(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    member_user: User,
    member_token: str,
    project_a: Project,
    private_project_c: Project,
    relations_role: Role,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
) -> None:
    """An issue in a private project the user is not a member of: 404, relation kept, no journals."""
    issue_a = await _create_issue(client, admin_token, project_a, lookups, "Visible task")
    issue_c = await _create_issue(client, admin_token, private_project_c, lookups, "Other project task")
    relation_id = await _create_relation(client, admin_token, issue_c["key"], issue_a["key"])
    await _grant(db_session, project_a, member_user, relations_role)

    journals_a = await _journal_count(db_session, issue_a["id"])
    journals_c = await _journal_count(db_session, issue_c["id"])

    resp = await client.delete(f"/api/v1/relations/{relation_id}/", headers=_auth(member_token))

    assert resp.status_code == 404, resp.text
    assert issue_c["key"] not in resp.text
    assert await _relation_exists(db_session, relation_id)
    assert await _journal_count(db_session, issue_a["id"]) == journals_a
    assert await _journal_count(db_session, issue_c["id"]) == journals_c


async def test_delete_allowed_when_both_visible_with_permission(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    member_user: User,
    member_token: str,
    project_a: Project,
    public_project_b: Project,
    relations_role: Role,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
) -> None:
    """Seeing both issues plus ``manage_issue_relations`` on one side is enough; journals are written."""
    issue_a = await _create_issue(client, admin_token, project_a, lookups, "Visible task")
    issue_b = await _create_issue(client, admin_token, public_project_b, lookups, "Public task")
    relation_id = await _create_relation(client, admin_token, issue_a["key"], issue_b["key"])
    await _grant(db_session, project_a, member_user, relations_role)

    journals_a = await _journal_count(db_session, issue_a["id"])
    journals_b = await _journal_count(db_session, issue_b["id"])

    resp = await client.delete(f"/api/v1/relations/{relation_id}/", headers=_auth(member_token))

    assert resp.status_code == 204, resp.text
    assert not await _relation_exists(db_session, relation_id)
    assert await _journal_count(db_session, issue_a["id"]) == journals_a + 1
    assert await _journal_count(db_session, issue_b["id"]) == journals_b + 1


async def test_delete_refused_without_manage_issue_relations(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    member_user: User,
    member_token: str,
    project_a: Project,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
) -> None:
    """Seeing both issues is not enough: ``edit_issues`` alone does not allow the delete."""
    issue_1 = await _create_issue(client, admin_token, project_a, lookups, "First task")
    issue_2 = await _create_issue(client, admin_token, project_a, lookups, "Second task")
    relation_id = await _create_relation(client, admin_token, issue_1["key"], issue_2["key"])
    editor_role = await _make_role(db_session, ["view_issues", "edit_issues"])
    await _grant(db_session, project_a, member_user, editor_role)

    journals_1 = await _journal_count(db_session, issue_1["id"])
    journals_2 = await _journal_count(db_session, issue_2["id"])

    resp = await client.delete(f"/api/v1/relations/{relation_id}/", headers=_auth(member_token))

    assert resp.status_code == 403, resp.text
    assert await _relation_exists(db_session, relation_id)
    assert await _journal_count(db_session, issue_1["id"]) == journals_1
    assert await _journal_count(db_session, issue_2["id"]) == journals_2
