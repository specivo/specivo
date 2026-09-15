"""Issue autocomplete applies the same visibility as issue listing.

Autocomplete returns issue keys and subjects, so it must hide exactly what
``IssueService`` issue listing hides:

- ``default``/``all`` role visibility: non-private issues plus private issues
  the user authored or is assigned to.
- ``own`` role visibility: only issues the user authored or is assigned to.
- Memberships held through a user group behave like direct memberships.
- Admins see everything.
- A non-member of a public project sees its non-private issues only.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.issue import Issue
from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.role import Role
from specivo.models.user import User
from specivo.models.user_group import UserGroup, UserGroupMember
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import TEST_PASSWORD, AdminUserFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

# Every subject starts with this marker so one query matches all test issues.
_MARK = "Acvis"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def lookups(db_session: AsyncSession) -> tuple[Tracker, IssueStatus, IssuePriority]:
    status = StatusFactory.build(name="AcvisNew", position=1, category="backlog")
    db_session.add(status)
    await db_session.flush()
    tracker = TrackerFactory.build(name="AcvisBug", default_status_id=status.id)
    priority = PriorityFactory.build(name="AcvisNormal", is_default=False, position=2)
    db_session.add_all([tracker, priority])
    await db_session.commit()
    return tracker, status, priority


@pytest_asyncio.fixture
async def private_project(db_session: AsyncSession) -> Project:
    project = ProjectFactory.build(key="ACVPRIV", identifier="acvis-private", is_public=False)
    db_session.add(project)
    await db_session.commit()
    return project


@pytest_asyncio.fixture
async def public_project(db_session: AsyncSession) -> Project:
    project = ProjectFactory.build(key="ACVPUB", identifier="acvis-public", is_public=True)
    db_session.add(project)
    await db_session.commit()
    return project


@pytest_asyncio.fixture
async def viewer(db_session: AsyncSession) -> User:
    user = UserFactory.build(login="acvis_viewer", status="active")
    db_session.add(user)
    await db_session.commit()
    return user


@pytest_asyncio.fixture
async def other(db_session: AsyncSession) -> User:
    user = UserFactory.build(login="acvis_other", status="active")
    db_session.add(user)
    await db_session.commit()
    return user


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _token(client: AsyncClient, login: str) -> str:
    resp = await client.post("/api/v1/auth/login/", json={"login": login, "password": TEST_PASSWORD})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


async def _grant(db: AsyncSession, project: Project, user: User, visibility: str, via: str) -> None:
    """Give *user* a role with *visibility* on *project*, directly or through a group."""
    role = Role(
        name=f"Acvis-{uuid.uuid4().hex[:8]}",
        position=2,
        assignable=True,
        builtin=0,
        permissions=["view_issues"],
        issues_visibility=visibility,
        settings={},
    )
    db.add(role)
    await db.flush()

    if via == "group":
        group = UserGroup(name=f"acvis-group-{uuid.uuid4().hex[:8]}")
        db.add(group)
        await db.flush()
        db.add(UserGroupMember(group_id=group.id, user_id=user.id))
        member = Member(project_id=project.id, group_id=group.id)
    else:
        member = Member(project_id=project.id, user_id=user.id)
    db.add(member)
    await db.flush()
    db.add(MemberRole(member_id=member.id, role_id=role.id))
    await db.commit()


_seq = iter(range(1, 10_000))


async def _issue(
    db: AsyncSession,
    project: Project,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
    *,
    subject: str,
    author: User,
    assignee: User | None = None,
    is_private: bool = False,
) -> Issue:
    tracker, status, priority = lookups
    issue = IssueFactory.build(
        project_id=project.id,
        project_key=project.key,
        sequence_number=next(_seq),
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=author.id,
        assigned_to_id=assignee.id if assignee else None,
        subject=f"{_MARK} {subject}",
        is_private=is_private,
    )
    db.add(issue)
    await db.commit()
    return issue


async def _subjects(client: AsyncClient, token: str) -> set[str]:
    resp = await client.get(
        "/api/v1/issues/autocomplete/",
        params={"q": _MARK, "limit": 25},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    return {item["subject"].removeprefix(f"{_MARK} ") for item in resp.json()}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("visibility", ["default", "all"])
@pytest.mark.parametrize("via", ["user", "group"])
async def test_member_does_not_see_other_users_private_issues(
    client: AsyncClient,
    db_session: AsyncSession,
    lookups,
    private_project: Project,
    viewer: User,
    other: User,
    visibility: str,
    via: str,
) -> None:
    """A member sees non-private issues and their own private ones, never someone else's."""
    await _grant(db_session, private_project, viewer, visibility, via)
    await _issue(db_session, private_project, lookups, subject="shared", author=other)
    await _issue(db_session, private_project, lookups, subject="others-private", author=other, is_private=True)
    await _issue(db_session, private_project, lookups, subject="authored-private", author=viewer, is_private=True)
    await _issue(
        db_session, private_project, lookups, subject="assigned-private", author=other, assignee=viewer, is_private=True
    )

    subjects = await _subjects(client, await _token(client, viewer.login))

    assert subjects == {"shared", "authored-private", "assigned-private"}


@pytest.mark.parametrize("via", ["user", "group"])
async def test_own_visibility_member_sees_only_own_issues(
    client: AsyncClient,
    db_session: AsyncSession,
    lookups,
    private_project: Project,
    viewer: User,
    other: User,
    via: str,
) -> None:
    """A role with ``own`` visibility limits autocomplete to authored or assigned issues."""
    await _grant(db_session, private_project, viewer, "own", via)
    await _issue(db_session, private_project, lookups, subject="others-public", author=other)
    await _issue(db_session, private_project, lookups, subject="others-private", author=other, is_private=True)
    await _issue(db_session, private_project, lookups, subject="authored", author=viewer)
    await _issue(db_session, private_project, lookups, subject="assigned", author=other, assignee=viewer)

    subjects = await _subjects(client, await _token(client, viewer.login))

    assert subjects == {"authored", "assigned"}


async def test_most_permissive_role_wins_across_direct_and_group_membership(
    client: AsyncClient,
    db_session: AsyncSession,
    lookups,
    private_project: Project,
    viewer: User,
    other: User,
) -> None:
    """``own`` held directly plus ``default`` held via a group resolves to ``default``, as listing does."""
    await _grant(db_session, private_project, viewer, "own", "user")
    await _grant(db_session, private_project, viewer, "default", "group")
    await _issue(db_session, private_project, lookups, subject="others-public", author=other)
    await _issue(db_session, private_project, lookups, subject="others-private", author=other, is_private=True)

    subjects = await _subjects(client, await _token(client, viewer.login))

    assert subjects == {"others-public"}


async def test_admin_sees_every_issue(
    client: AsyncClient,
    db_session: AsyncSession,
    lookups,
    private_project: Project,
    public_project: Project,
    other: User,
) -> None:
    admin = AdminUserFactory.build(login="acvis_admin", status="active")
    db_session.add(admin)
    await db_session.commit()
    await _issue(db_session, private_project, lookups, subject="private-project", author=other)
    await _issue(db_session, private_project, lookups, subject="private-issue", author=other, is_private=True)
    await _issue(db_session, public_project, lookups, subject="public-private-issue", author=other, is_private=True)

    subjects = await _subjects(client, await _token(client, admin.login))

    assert subjects == {"private-project", "private-issue", "public-private-issue"}


async def test_non_member_sees_only_non_private_issues_of_public_projects(
    client: AsyncClient,
    db_session: AsyncSession,
    lookups,
    private_project: Project,
    public_project: Project,
    viewer: User,
    other: User,
) -> None:
    await _issue(db_session, public_project, lookups, subject="public-shared", author=other)
    await _issue(db_session, public_project, lookups, subject="public-private", author=other, is_private=True)
    await _issue(db_session, private_project, lookups, subject="private-project", author=other)

    subjects = await _subjects(client, await _token(client, viewer.login))

    assert subjects == {"public-shared"}


async def test_member_of_one_project_is_still_a_non_member_elsewhere(
    client: AsyncClient,
    db_session: AsyncSession,
    lookups,
    private_project: Project,
    public_project: Project,
    viewer: User,
    other: User,
) -> None:
    """Membership on one project does not change what a public project exposes."""
    await _grant(db_session, private_project, viewer, "own", "user")
    await _issue(db_session, public_project, lookups, subject="public-shared", author=other)
    await _issue(db_session, public_project, lookups, subject="public-private", author=other, is_private=True)
    await _issue(db_session, private_project, lookups, subject="member-others", author=other)

    subjects = await _subjects(client, await _token(client, viewer.login))

    assert subjects == {"public-shared"}
