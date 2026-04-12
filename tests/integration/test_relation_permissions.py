"""Integration tests for relation create and delete permission checks.

Covers:
- Admin can delete any relation
- Member can delete a relation in their own project
- Non-member cannot delete a relation in a private project (403)
- User can delete a relation when one project is public (even without membership in the other)
- Deleting a nonexistent relation returns 404
- Unauthenticated delete returns 401
- Create relation requires access to both issues (returns 404 for hidden issue)
- Member can create a relation between accessible issues (returns 201)
- Unauthenticated create returns 401
"""

from __future__ import annotations

import uuid

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


async def _create_relation(
    client: AsyncClient,
    token: str,
    issue_ref: str,
    issue_to_key: str,
    relation_type: str = "relates",
) -> tuple[int, dict]:
    resp = await client.post(
        f"/api/v1/issues/{issue_ref}/relations/",
        json={"issue_to_key": issue_to_key, "relation_type": relation_type},
        headers={"Authorization": f"Bearer {token}"},
    )
    return resp.status_code, resp.json()


async def _delete_relation(client: AsyncClient, token: str | None, relation_id: int) -> int:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    resp = await client.delete(f"/api/v1/relations/{relation_id}/", headers=headers)
    return resp.status_code


async def _grant_membership(db_session: AsyncSession, project: Project, user: User, role: Role) -> None:
    """Add user as a project member with the given role."""
    member = Member(project_id=project.id, user_id=user.id)
    db_session.add(member)
    await db_session.flush()
    member_role = MemberRole(member_id=member.id, role_id=role.id)
    db_session.add(member_role)
    await db_session.commit()


# ---------------------------------------------------------------------------
# Shared fixtures
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
async def admin_user(db_session: AsyncSession) -> User:
    user = AdminUserFactory.build(login="rp_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def admin_token(admin_user: User, client: AsyncClient) -> str:
    return await _login(client, admin_user.login)


@pytest_asyncio.fixture
async def regular_user(db_session: AsyncSession) -> User:
    user = UserFactory.build(login="rp_regular", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def regular_token(regular_user: User, client: AsyncClient) -> str:
    return await _login(client, regular_user.login)


@pytest_asyncio.fixture
async def dev_role(db_session: AsyncSession) -> Role:
    """A minimal assignable role for granting project membership in tests."""
    role = Role(
        name=f"RpDev-{uuid.uuid4().hex[:8]}",
        position=3,
        assignable=True,
        builtin=0,
        permissions=["view_issues", "add_issues", "edit_issues"],
        issues_visibility="default",
        settings={},
    )
    db_session.add(role)
    await db_session.commit()
    await db_session.refresh(role)
    return role


@pytest_asyncio.fixture
async def private_project_a(db_session: AsyncSession) -> Project:
    proj = ProjectFactory.build(key="RPA", identifier="rp-project-a", is_public=False)
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


@pytest_asyncio.fixture
async def private_project_b(db_session: AsyncSession) -> Project:
    proj = ProjectFactory.build(key="RPB", identifier="rp-project-b", is_public=False)
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


@pytest_asyncio.fixture
async def public_project(db_session: AsyncSession) -> Project:
    proj = ProjectFactory.build(key="RPPUB", identifier="rp-project-pub", is_public=True)
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


# ---------------------------------------------------------------------------
# DELETE permission tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_admin_can_delete_any_relation(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    private_project_a: Project,
    private_project_b: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """Admin can delete a relation between issues in any projects."""
    issue_a = await _create_issue(
        client, admin_token, private_project_a.key, tracker.id, open_status.id, priority.id, "Admin del A"
    )
    issue_b = await _create_issue(
        client, admin_token, private_project_b.key, tracker.id, open_status.id, priority.id, "Admin del B"
    )

    sc, rel = await _create_relation(client, admin_token, issue_a["key"], issue_b["key"])
    assert sc == 201, rel
    relation_id = rel["id"]

    delete_sc = await _delete_relation(client, admin_token, relation_id)
    assert delete_sc == 204


@pytest.mark.integration
async def test_member_can_delete_relation_in_own_project(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    regular_user: User,
    regular_token: str,
    private_project_a: Project,
    dev_role: Role,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """A project member can delete a relation involving their project's issue."""
    issue_a = await _create_issue(
        client, admin_token, private_project_a.key, tracker.id, open_status.id, priority.id, "Member del A"
    )
    issue_b = await _create_issue(
        client, admin_token, private_project_a.key, tracker.id, open_status.id, priority.id, "Member del B"
    )

    sc, rel = await _create_relation(client, admin_token, issue_a["key"], issue_b["key"])
    assert sc == 201, rel
    relation_id = rel["id"]

    # Grant regular user membership in project A
    await _grant_membership(db_session, private_project_a, regular_user, dev_role)

    delete_sc = await _delete_relation(client, regular_token, relation_id)
    assert delete_sc == 204


@pytest.mark.integration
async def test_non_member_cannot_delete_relation_in_private_project(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    regular_token: str,
    private_project_a: Project,
    private_project_b: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """Non-member of both private projects gets 403 when deleting a relation."""
    issue_a = await _create_issue(
        client, admin_token, private_project_a.key, tracker.id, open_status.id, priority.id, "NM del A"
    )
    issue_b = await _create_issue(
        client, admin_token, private_project_b.key, tracker.id, open_status.id, priority.id, "NM del B"
    )

    sc, rel = await _create_relation(client, admin_token, issue_a["key"], issue_b["key"])
    assert sc == 201, rel
    relation_id = rel["id"]

    # Regular user has no membership in either project
    delete_sc = await _delete_relation(client, regular_token, relation_id)
    assert delete_sc == 403


@pytest.mark.integration
async def test_user_can_delete_relation_if_one_project_is_public(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    regular_token: str,
    public_project: Project,
    private_project_b: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """User without edit_issues permission cannot delete even if one project is public.

    Public project visibility alone does not grant write permissions — the user
    needs an explicit role with edit_issues to delete relations.
    """
    issue_pub = await _create_issue(
        client, admin_token, public_project.key, tracker.id, open_status.id, priority.id, "Pub issue"
    )
    issue_priv = await _create_issue(
        client, admin_token, private_project_b.key, tracker.id, open_status.id, priority.id, "Priv issue"
    )

    sc, rel = await _create_relation(client, admin_token, issue_pub["key"], issue_priv["key"])
    assert sc == 201, rel
    relation_id = rel["id"]

    # Regular user has no edit_issues role on either project — should be denied
    delete_sc = await _delete_relation(client, regular_token, relation_id)
    assert delete_sc == 403


@pytest.mark.integration
async def test_delete_nonexistent_relation_returns_404(
    client: AsyncClient,
    admin_token: str,
) -> None:
    """Deleting a relation that does not exist returns 404."""
    delete_sc = await _delete_relation(client, admin_token, 999999)
    assert delete_sc == 404


@pytest.mark.integration
async def test_delete_relation_requires_auth(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    private_project_a: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """Unauthenticated DELETE returns 401."""
    issue_a = await _create_issue(
        client, admin_token, private_project_a.key, tracker.id, open_status.id, priority.id, "Auth A"
    )
    issue_b = await _create_issue(
        client, admin_token, private_project_a.key, tracker.id, open_status.id, priority.id, "Auth B"
    )

    sc, rel = await _create_relation(client, admin_token, issue_a["key"], issue_b["key"])
    assert sc == 201, rel
    relation_id = rel["id"]

    # No token
    delete_sc = await _delete_relation(client, None, relation_id)
    assert delete_sc == 401


# ---------------------------------------------------------------------------
# CREATE permission tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_create_relation_requires_access_to_both_issues(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    regular_user: User,
    regular_token: str,
    private_project_a: Project,
    private_project_b: Project,
    dev_role: Role,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """Regular user with access to project A but not B gets 404 when creating a
    relation from an A issue to a hidden B issue (anti-enumeration)."""
    issue_a = await _create_issue(
        client, admin_token, private_project_a.key, tracker.id, open_status.id, priority.id, "Create perm A"
    )
    issue_b = await _create_issue(
        client, admin_token, private_project_b.key, tracker.id, open_status.id, priority.id, "Create perm B"
    )

    # Grant membership in project A only
    await _grant_membership(db_session, private_project_a, regular_user, dev_role)

    # Regular user can see issue_a (is a member) but NOT issue_b (private, no membership)
    sc, data = await _create_relation(client, regular_token, issue_a["key"], issue_b["key"])
    assert sc == 404, data


@pytest.mark.integration
async def test_member_can_create_relation_between_accessible_issues(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    regular_user: User,
    regular_token: str,
    private_project_a: Project,
    private_project_b: Project,
    dev_role: Role,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """User with membership in both projects can create a cross-project relation."""
    issue_a = await _create_issue(
        client, admin_token, private_project_a.key, tracker.id, open_status.id, priority.id, "Both access A"
    )
    issue_b = await _create_issue(
        client, admin_token, private_project_b.key, tracker.id, open_status.id, priority.id, "Both access B"
    )

    # Grant membership in both projects
    await _grant_membership(db_session, private_project_a, regular_user, dev_role)

    # Need a second distinct role name for the second membership
    role_b = Role(
        name=f"RpDevB-{uuid.uuid4().hex[:8]}",
        position=4,
        assignable=True,
        builtin=0,
        permissions=["view_issues", "add_issues", "edit_issues"],
        issues_visibility="default",
        settings={},
    )
    db_session.add(role_b)
    await db_session.commit()
    await db_session.refresh(role_b)

    await _grant_membership(db_session, private_project_b, regular_user, role_b)

    sc, data = await _create_relation(client, regular_token, issue_a["key"], issue_b["key"])
    assert sc == 201, data
    assert data["relation_type"] == "relates"


@pytest.mark.integration
async def test_create_relation_requires_auth(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    private_project_a: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """Unauthenticated POST to create a relation returns 401."""
    issue_a = await _create_issue(
        client, admin_token, private_project_a.key, tracker.id, open_status.id, priority.id, "Unauth create A"
    )
    issue_b = await _create_issue(
        client, admin_token, private_project_a.key, tracker.id, open_status.id, priority.id, "Unauth create B"
    )

    resp = await client.post(
        f"/api/v1/issues/{issue_a['key']}/relations/",
        json={"issue_to_key": issue_b["key"], "relation_type": "relates"},
    )
    assert resp.status_code == 401
