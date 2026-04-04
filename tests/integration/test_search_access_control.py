"""Integration tests for search access control / visibility enforcement.

Verifies that search results respect project membership, role visibility
settings (all / own / default), and private issue rules. Uses the same
helper patterns as test_search_fts.py.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.member import Member, MemberRole
from specivo.models.project import EnabledModule, Project
from specivo.models.role import Role
from specivo.models.security_audit import SecurityAuditLog
from specivo.models.user import User
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import TEST_PASSWORD, UserFactory

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SEARCH_URL = "/api/v1/search/"


async def _make_user(db: AsyncSession, login: str) -> User:
    user = UserFactory.build(login=login, status="active")
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _login(client: AsyncClient, login: str) -> str:
    resp = await client.post(
        "/api/v1/auth/login/",
        json={"login": login, "password": TEST_PASSWORD},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


async def _make_project(
    db: AsyncSession,
    key: str,
    identifier: str,
    is_public: bool = True,
) -> Project:
    proj = ProjectFactory.build(key=key, identifier=identifier, is_public=is_public)
    db.add(proj)
    await db.commit()
    await db.refresh(proj)
    return proj


async def _seed_lookups(
    db: AsyncSession,
) -> tuple[Tracker, IssueStatus, IssuePriority]:
    status = StatusFactory.build(name="New", position=1, is_closed=False)
    db.add(status)
    await db.flush()
    tracker = TrackerFactory.build(name="Bug", default_status_id=status.id)
    db.add(tracker)
    priority = PriorityFactory.build(name="Normal", is_default=True, position=2)
    db.add(priority)
    await db.commit()
    await db.refresh(status)
    await db.refresh(tracker)
    await db.refresh(priority)
    return tracker, status, priority


async def _enable_wiki(db: AsyncSession, project: Project) -> None:
    db.add(EnabledModule(project_id=project.id, name="wiki"))
    await db.commit()


async def _add_member(
    db: AsyncSession,
    project: Project,
    user: User,
    permissions: list[str] | None = None,
    issues_visibility: str = "default",
) -> None:
    """Add user as project member with specified role settings."""
    if permissions is None:
        permissions = ["*"]
    role = Role(
        name=f"Role-{project.key}-{user.id}",
        permissions=permissions,
        builtin=0,
        issues_visibility=issues_visibility,
    )
    db.add(role)
    await db.flush()
    member = Member(user_id=user.id, project_id=project.id)
    db.add(member)
    await db.flush()
    mr = MemberRole(member_id=member.id, role_id=role.id)
    db.add(mr)
    await db.commit()


async def _create_issue(
    client: AsyncClient,
    project_key: str,
    tracker_id: int,
    subject: str,
    description: str | None = None,
    is_private: bool = False,
) -> dict:
    payload: dict = {
        "project_key": project_key,
        "tracker_id": tracker_id,
        "subject": subject,
    }
    if description is not None:
        payload["description"] = description
    if is_private:
        payload["is_private"] = True
    resp = await client.post(f"/api/v1/projects/{project_key}/issues/", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_wiki_page(
    client: AsyncClient,
    project_key: str,
    title: str,
    text: str,
) -> dict:
    resp = await client.post(
        f"/api/v1/projects/{project_key}/wiki/",
        json={"title": title, "text": text},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _search(client: AsyncClient, q: str, **params) -> dict:
    resp = await client.get(SEARCH_URL, params={"q": q, **params})
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def lookups(db_session: AsyncSession) -> tuple[Tracker, IssueStatus, IssuePriority]:
    return await _seed_lookups(db_session)


@pytest_asyncio.fixture
async def public_project(db_session: AsyncSession) -> Project:
    return await _make_project(db_session, key="PUB", identifier="public-project", is_public=True)


@pytest_asyncio.fixture
async def private_project(db_session: AsyncSession) -> Project:
    return await _make_project(db_session, key="PRV", identifier="private-project", is_public=False)


@pytest_asyncio.fixture
async def admin_user(db_session: AsyncSession) -> User:
    from tests.factories.user import AdminUserFactory

    user = AdminUserFactory.build(login="acl_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def member_user(db_session: AsyncSession) -> User:
    return await _make_user(db_session, login="acl_member")


@pytest_asyncio.fixture
async def other_user(db_session: AsyncSession) -> User:
    return await _make_user(db_session, login="acl_other")


# ---------------------------------------------------------------------------
# Tests — Admin visibility
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_sees_all_issues_in_search(
    db_session: AsyncSession,
    client: AsyncClient,
    public_project: Project,
    private_project: Project,
    admin_user: User,
    member_user: User,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
):
    """Admin sees issues from all projects (public and private), including private issues."""
    tracker, _, _ = lookups

    # Add member to public project so they can create issues
    await _add_member(db_session, public_project, member_user)
    await _add_member(db_session, private_project, member_user)

    member_token = await _login(client, member_user.login)
    client.headers["Authorization"] = f"Bearer {member_token}"

    await _create_issue(client, public_project.key, tracker.id, "Visibility admin public alpha")
    await _create_issue(client, private_project.key, tracker.id, "Visibility admin private alpha")
    await _create_issue(client, public_project.key, tracker.id, "Visibility admin secret alpha", is_private=True)

    # Switch to admin
    admin_token = await _login(client, admin_user.login)
    client.headers["Authorization"] = f"Bearer {admin_token}"

    data = await _search(client, "visibility admin alpha", scope="issues")
    assert data["total_count"] == 3


# ---------------------------------------------------------------------------
# Tests — Member with "all" visibility
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_member_all_visibility_sees_non_private(
    db_session: AsyncSession,
    client: AsyncClient,
    public_project: Project,
    admin_user: User,
    member_user: User,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
):
    """Member with issues_visibility='all' sees non-private issues."""
    tracker, _, _ = lookups
    await _add_member(db_session, public_project, admin_user)
    await _add_member(db_session, public_project, member_user, issues_visibility="all")

    admin_token = await _login(client, admin_user.login)
    client.headers["Authorization"] = f"Bearer {admin_token}"
    await _create_issue(client, public_project.key, tracker.id, "Visibility allvis nonpriv beta")

    member_token = await _login(client, member_user.login)
    client.headers["Authorization"] = f"Bearer {member_token}"

    data = await _search(client, "visibility allvis nonpriv beta", scope="issues")
    assert data["total_count"] == 1


@pytest.mark.asyncio
async def test_member_all_visibility_sees_own_private(
    db_session: AsyncSession,
    client: AsyncClient,
    public_project: Project,
    member_user: User,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
):
    """Member with issues_visibility='all' sees their own private issues."""
    tracker, _, _ = lookups
    await _add_member(db_session, public_project, member_user, issues_visibility="all")

    member_token = await _login(client, member_user.login)
    client.headers["Authorization"] = f"Bearer {member_token}"
    await _create_issue(client, public_project.key, tracker.id, "Visibility ownpriv gamma", is_private=True)

    data = await _search(client, "visibility ownpriv gamma", scope="issues")
    assert data["total_count"] == 1


@pytest.mark.asyncio
async def test_member_all_visibility_hides_others_private(
    db_session: AsyncSession,
    client: AsyncClient,
    public_project: Project,
    admin_user: User,
    member_user: User,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
):
    """Member with issues_visibility='all' cannot see other users' private issues."""
    tracker, _, _ = lookups
    await _add_member(db_session, public_project, admin_user)
    await _add_member(db_session, public_project, member_user, issues_visibility="all")

    # Admin creates a private issue
    admin_token = await _login(client, admin_user.login)
    client.headers["Authorization"] = f"Bearer {admin_token}"
    await _create_issue(client, public_project.key, tracker.id, "Visibility otherpriv delta", is_private=True)

    # Member should not see it
    member_token = await _login(client, member_user.login)
    client.headers["Authorization"] = f"Bearer {member_token}"

    data = await _search(client, "visibility otherpriv delta", scope="issues")
    assert data["total_count"] == 0


# ---------------------------------------------------------------------------
# Tests — Member with "own" visibility
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_member_own_visibility_sees_only_own(
    db_session: AsyncSession,
    client: AsyncClient,
    public_project: Project,
    admin_user: User,
    member_user: User,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
):
    """Member with issues_visibility='own' sees only issues they authored."""
    tracker, _, _ = lookups
    await _add_member(db_session, public_project, admin_user)
    await _add_member(db_session, public_project, member_user, issues_visibility="own")

    # Admin creates an issue
    admin_token = await _login(client, admin_user.login)
    client.headers["Authorization"] = f"Bearer {admin_token}"
    await _create_issue(client, public_project.key, tracker.id, "Visibility ownonly epsilon admin")

    # Member creates an issue
    member_token = await _login(client, member_user.login)
    client.headers["Authorization"] = f"Bearer {member_token}"
    await _create_issue(client, public_project.key, tracker.id, "Visibility ownonly epsilon member")

    data = await _search(client, "visibility ownonly epsilon", scope="issues")
    assert data["total_count"] == 1
    assert "member" in data["items"][0]["subtitle"].lower()


# ---------------------------------------------------------------------------
# Tests — Non-member visibility
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_member_public_project_sees_non_private(
    db_session: AsyncSession,
    client: AsyncClient,
    public_project: Project,
    admin_user: User,
    other_user: User,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
):
    """Non-member can search non-private issues in public projects."""
    tracker, _, _ = lookups
    await _add_member(db_session, public_project, admin_user)

    admin_token = await _login(client, admin_user.login)
    client.headers["Authorization"] = f"Bearer {admin_token}"
    await _create_issue(client, public_project.key, tracker.id, "Visibility nonmem zeta public")

    other_token = await _login(client, other_user.login)
    client.headers["Authorization"] = f"Bearer {other_token}"

    data = await _search(client, "visibility nonmem zeta public", scope="issues")
    assert data["total_count"] == 1


@pytest.mark.asyncio
async def test_non_member_private_project_sees_nothing(
    db_session: AsyncSession,
    client: AsyncClient,
    private_project: Project,
    admin_user: User,
    other_user: User,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
):
    """Non-member cannot see any issues from a private project in search results."""
    tracker, _, _ = lookups
    await _add_member(db_session, private_project, admin_user)

    admin_token = await _login(client, admin_user.login)
    client.headers["Authorization"] = f"Bearer {admin_token}"
    await _create_issue(client, private_project.key, tracker.id, "Visibility nonmem eta private")

    other_token = await _login(client, other_user.login)
    client.headers["Authorization"] = f"Bearer {other_token}"

    data = await _search(client, "visibility nonmem eta private", scope="issues")
    assert data["total_count"] == 0


# ---------------------------------------------------------------------------
# Tests — Cross-project and wiki
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_project_search_respects_visibility(
    db_session: AsyncSession,
    client: AsyncClient,
    public_project: Project,
    private_project: Project,
    member_user: User,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
):
    """Cross-project search only returns results from projects the user can access."""
    tracker, _, _ = lookups

    # Member has access to public but not private
    await _add_member(db_session, public_project, member_user)

    member_token = await _login(client, member_user.login)
    client.headers["Authorization"] = f"Bearer {member_token}"

    await _create_issue(client, public_project.key, tracker.id, "Crossproj theta visible")

    # Create issue in private project via admin workaround - use DB directly
    from tests.factories.user import AdminUserFactory

    admin = AdminUserFactory.build(login="cross_admin", status="active")
    db_session.add(admin)
    await db_session.commit()
    await db_session.refresh(admin)
    await _add_member(db_session, private_project, admin)

    admin_token = await _login(client, admin.login)
    client.headers["Authorization"] = f"Bearer {admin_token}"
    await _create_issue(client, private_project.key, tracker.id, "Crossproj theta hidden")

    # Switch back to member
    client.headers["Authorization"] = f"Bearer {member_token}"
    data = await _search(client, "crossproj theta", scope="issues")

    # Should only see 1 result from public project
    assert data["total_count"] == 1
    assert data["items"][0]["project_key"] == public_project.key


@pytest.mark.asyncio
async def test_wiki_search_respects_project_membership(
    db_session: AsyncSession,
    client: AsyncClient,
    public_project: Project,
    private_project: Project,
    member_user: User,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
):
    """Wiki search results are filtered by project membership."""
    await _enable_wiki(db_session, public_project)
    await _enable_wiki(db_session, private_project)
    await _add_member(db_session, public_project, member_user)

    from tests.factories.user import AdminUserFactory

    admin = AdminUserFactory.build(login="wiki_acl_admin", status="active")
    db_session.add(admin)
    await db_session.commit()
    await db_session.refresh(admin)
    await _add_member(db_session, private_project, admin)
    await _add_member(db_session, public_project, admin)

    # Admin creates wiki pages in both projects
    admin_token = await _login(client, admin.login)
    client.headers["Authorization"] = f"Bearer {admin_token}"
    await _create_wiki_page(
        client,
        public_project.key,
        "Iota Guide Public",
        "Iota documentation for the public project",
    )
    await _create_wiki_page(
        client,
        private_project.key,
        "Iota Guide Private",
        "Iota documentation for the private project",
    )

    # Member searches — should only see public project wiki
    member_token = await _login(client, member_user.login)
    client.headers["Authorization"] = f"Bearer {member_token}"

    data = await _search(client, "iota documentation", scope="wiki")
    assert data["total_count"] == 1
    assert data["items"][0]["project_key"] == public_project.key


# ---------------------------------------------------------------------------
# Tests — Audit logging of search queries
# ---------------------------------------------------------------------------


@pytest.mark.enterprise
@pytest.mark.asyncio
async def test_search_query_logged_in_audit(
    db_session: AsyncSession,
    client: AsyncClient,
    public_project: Project,
    member_user: User,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
):
    """Every search query creates a search_query audit log entry."""
    await _add_member(db_session, public_project, member_user)
    member_token = await _login(client, member_user.login)
    client.headers["Authorization"] = f"Bearer {member_token}"

    await _search(client, "kappa test query")

    stmt = select(SecurityAuditLog).where(
        SecurityAuditLog.event_type == "search_query",
        SecurityAuditLog.user_id == member_user.id,
    )
    result = await db_session.execute(stmt)
    log = result.scalar_one()

    assert log.details["query"] == "kappa test query"


# ---------------------------------------------------------------------------
# Tests — Multi-project search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multi_project_search(
    db_session: AsyncSession,
    client: AsyncClient,
    member_user: User,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
):
    """Search with project_keys=KEY1,KEY2 scopes to multiple projects."""
    tracker, _, _ = lookups

    proj_a = await _make_project(db_session, key="MPA", identifier="multi-proj-a")
    proj_b = await _make_project(db_session, key="MPB", identifier="multi-proj-b")
    proj_c = await _make_project(db_session, key="MPC", identifier="multi-proj-c")

    await _add_member(db_session, proj_a, member_user)
    await _add_member(db_session, proj_b, member_user)
    await _add_member(db_session, proj_c, member_user)

    member_token = await _login(client, member_user.login)
    client.headers["Authorization"] = f"Bearer {member_token}"

    await _create_issue(client, proj_a.key, tracker.id, "Lambda multiproj searchtest")
    await _create_issue(client, proj_b.key, tracker.id, "Lambda multiproj searchtest")
    await _create_issue(client, proj_c.key, tracker.id, "Lambda multiproj searchtest")

    # Search scoped to proj_a and proj_b only
    data = await _search(
        client,
        "lambda multiproj searchtest",
        project_keys=f"{proj_a.key},{proj_b.key}",
        scope="issues",
    )
    assert data["total_count"] == 2
    returned_keys = {item["project_key"] for item in data["items"]}
    assert returned_keys == {proj_a.key, proj_b.key}
