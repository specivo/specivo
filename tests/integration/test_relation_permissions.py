"""Integration tests for relation create and delete permission checks.

Covers:
- Admin can delete any relation
- Member can delete a relation in their own project
- Non-member of both private projects gets 404 when deleting (neither issue is visible)
- A relation from a public issue to a hidden private-project issue answers 404
- Deleting a nonexistent relation returns 404
- Unauthenticated delete returns 401
- Create relation requires access to both issues (returns 404 for hidden issue)
- Member can create a relation between accessible issues (returns 201)
- Create relation requires manage_issue_relations on the source issue's project (returns 403
  for a signed-in non-member of a public project and for a role with edit_issues only)
- Unauthenticated create returns 401
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


async def _relation_and_journal_counts(db_session: AsyncSession, *issue_ids: int) -> tuple[int, int]:
    """Count relations touching, and journal entries on, the given issues."""
    relations = await db_session.execute(
        select(func.count())
        .select_from(IssueRelation)
        .where(IssueRelation.issue_from_id.in_(issue_ids) | IssueRelation.issue_to_id.in_(issue_ids))
    )
    journals = await db_session.execute(
        select(func.count()).select_from(Journal).where(Journal.issue_id.in_(issue_ids))
    )
    return relations.scalar_one(), journals.scalar_one()


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
        permissions=["view_issues", "add_issues", "edit_issues", "manage_issue_relations"],
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
    """Non-member of both private projects gets 404: neither linked issue is visible."""
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
    assert delete_sc == 404


@pytest.mark.integration
async def test_user_cannot_delete_relation_to_hidden_issue_via_public_project(
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
    """Seeing the public side is not enough: the private-project issue stays hidden (404).

    Deleting a relation requires seeing both linked issues, so the response is
    the same as for a relation that does not exist.
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

    # Regular user cannot see the private-project issue — answered as not found
    delete_sc = await _delete_relation(client, regular_token, relation_id)
    assert delete_sc == 404


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


@pytest.mark.integration
async def test_non_member_of_public_project_cannot_create_relation(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    regular_token: str,
    public_project: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """Seeing two public issues is not enough: a non-member gets 403 and nothing is written."""
    issue_a = await _create_issue(
        client, admin_token, public_project.key, tracker.id, open_status.id, priority.id, "Public create A"
    )
    issue_b = await _create_issue(
        client, admin_token, public_project.key, tracker.id, open_status.id, priority.id, "Public create B"
    )
    resp = await client.get(f"/api/v1/issues/{issue_a['key']}/", headers={"Authorization": f"Bearer {regular_token}"})
    assert resp.status_code == 200, "precondition: the non-member can read the public issue"
    before = await _relation_and_journal_counts(db_session, issue_a["id"], issue_b["id"])

    sc, data = await _create_relation(client, regular_token, issue_a["key"], issue_b["key"])

    assert sc == 403, data
    assert await _relation_and_journal_counts(db_session, issue_a["id"], issue_b["id"]) == before


@pytest.mark.integration
async def test_edit_issues_without_manage_issue_relations_cannot_create_relation(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    regular_user: User,
    regular_token: str,
    private_project_a: Project,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """A member whose role can edit issues but not manage relations gets 403."""
    editor_role = Role(
        name=f"RpEditor-{uuid.uuid4().hex[:8]}",
        position=5,
        assignable=True,
        builtin=0,
        permissions=["view_issues", "add_issues", "edit_issues"],
        issues_visibility="default",
        settings={},
    )
    db_session.add(editor_role)
    await db_session.commit()
    await _grant_membership(db_session, private_project_a, regular_user, editor_role)
    issue_a = await _create_issue(
        client, admin_token, private_project_a.key, tracker.id, open_status.id, priority.id, "Editor create A"
    )
    issue_b = await _create_issue(
        client, admin_token, private_project_a.key, tracker.id, open_status.id, priority.id, "Editor create B"
    )
    before = await _relation_and_journal_counts(db_session, issue_a["id"], issue_b["id"])

    sc, data = await _create_relation(client, regular_token, issue_a["key"], issue_b["key"])

    assert sc == 403, data
    assert await _relation_and_journal_counts(db_session, issue_a["id"], issue_b["id"]) == before


@pytest.mark.integration
async def test_member_with_manage_issue_relations_can_create_relation(
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
    """The permission on the source issue's project is enough; journals are written on both issues."""
    await _grant_membership(db_session, private_project_a, regular_user, dev_role)
    issue_a = await _create_issue(
        client, admin_token, private_project_a.key, tracker.id, open_status.id, priority.id, "Manager create A"
    )
    issue_b = await _create_issue(
        client, admin_token, private_project_a.key, tracker.id, open_status.id, priority.id, "Manager create B"
    )
    relations_before, journals_before = await _relation_and_journal_counts(db_session, issue_a["id"], issue_b["id"])

    sc, data = await _create_relation(client, regular_token, issue_a["key"], issue_b["key"])

    assert sc == 201, data
    assert await _relation_and_journal_counts(db_session, issue_a["id"], issue_b["id"]) == (
        relations_before + 1,
        journals_before + 2,
    )


# ---------------------------------------------------------------------------
# Web controls follow the same permission
# ---------------------------------------------------------------------------


async def _page(client: AsyncClient, token: str, path: str) -> str:
    resp = await client.get(path, cookies={"access_token": token})
    assert resp.status_code == 200, resp.text[:300]
    return resp.text


@pytest.mark.integration
async def test_issue_pages_show_relation_controls_only_with_manage_issue_relations(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    regular_user: User,
    regular_token: str,
    public_project: Project,
    dev_role: Role,
    tracker: Tracker,
    open_status: IssueStatus,
    priority: IssuePriority,
) -> None:
    """A reader the relations API would refuse is not offered the add or remove controls."""
    issue_a = await _create_issue(
        client, admin_token, public_project.key, tracker.id, open_status.id, priority.id, "Controls A"
    )
    issue_b = await _create_issue(
        client, admin_token, public_project.key, tracker.id, open_status.id, priority.id, "Controls B"
    )
    sc, data = await _create_relation(client, admin_token, issue_a["key"], issue_b["key"])
    assert sc == 201, data

    detail = f"/issue/{issue_a['key']}/"
    new_form = f"/projects/{public_project.key}/issues/new/"

    # Signed-in non-member of the public project: can read, cannot manage relations.
    page = await _page(client, regular_token, detail)
    assert issue_b["key"] in page, "precondition: the relation itself is listed"
    assert "relationForm(" not in page
    assert "sp-rel-remove" not in page
    assert "pendingRelations" not in await _page(client, regular_token, new_form)

    # Once a member with the permission, the same reader gets the controls.
    await _grant_membership(db_session, public_project, regular_user, dev_role)
    page = await _page(client, regular_token, detail)
    assert "relationForm(" in page
    assert "sp-rel-remove" in page
    assert "pendingRelations" in await _page(client, regular_token, new_form)
