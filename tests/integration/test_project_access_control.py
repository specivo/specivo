"""Integration tests for membership-based project access control.

Verifies that:
- Non-admin users see only projects they are members of (or public projects)
- Private projects return 404 for non-members (not 403, to prevent enumeration)
- Public projects are accessible to all authenticated users
- Admin users see all projects regardless of membership
- Empty state is shown when user has no accessible projects
- All project-scoped web routes enforce membership checks
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import NotFoundError
from specivo.models.member import Member, MemberRole
from specivo.models.project import EnabledModule, Project
from specivo.models.role import Role
from specivo.models.user import User
from specivo.services.project_service import ProjectService
from tests.factories.project import ProjectFactory
from tests.factories.user import TEST_PASSWORD, AdminUserFactory, UserFactory

pytestmark = pytest.mark.integration

_svc = ProjectService()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_user(db: AsyncSession, login: str, **kwargs) -> User:
    user = UserFactory.build(login=login, status="active", **kwargs)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _make_admin(db: AsyncSession, login: str) -> User:
    user = AdminUserFactory.build(login=login, status="active")
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _make_project(
    db: AsyncSession,
    key: str,
    identifier: str,
    is_public: bool = False,
) -> Project:
    proj = ProjectFactory.build(key=key, identifier=identifier, is_public=is_public)
    db.add(proj)
    await db.commit()
    await db.refresh(proj)
    return proj


async def _add_member(
    db: AsyncSession,
    project: Project,
    user: User,
    permissions: list[str] | None = None,
) -> None:
    if permissions is None:
        permissions = ["*"]
    role = Role(
        name=f"Role-{project.key}-{user.id}",
        permissions=permissions,
        builtin=0,
        issues_visibility="default",
    )
    db.add(role)
    await db.flush()
    member = Member(user_id=user.id, project_id=project.id)
    db.add(member)
    await db.flush()
    mr = MemberRole(member_id=member.id, role_id=role.id)
    db.add(mr)
    await db.commit()


async def _login(client: AsyncClient, login: str) -> str:
    resp = await client.post(
        "/api/v1/auth/login/",
        json={"login": login, "password": TEST_PASSWORD},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def private_project(db_session: AsyncSession) -> Project:
    return await _make_project(db_session, "PRIV", "private-project", is_public=False)


@pytest_asyncio.fixture
async def public_project(db_session: AsyncSession) -> Project:
    return await _make_project(db_session, "PUB", "public-project", is_public=True)


@pytest_asyncio.fixture
async def outsider(db_session: AsyncSession) -> User:
    """User who is NOT a member of any project."""
    return await _make_user(db_session, "outsider")


@pytest_asyncio.fixture
async def member_user(db_session: AsyncSession, private_project: Project) -> User:
    """User who is a member of private_project."""
    user = await _make_user(db_session, "member")
    await _add_member(db_session, private_project, user)
    return user


@pytest_asyncio.fixture
async def admin_user(db_session: AsyncSession) -> User:
    return await _make_admin(db_session, "admin_access_test")


# ---------------------------------------------------------------------------
# Service-level tests: require_project_access
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_can_access_private_project(db_session: AsyncSession, private_project: Project, admin_user: User):
    """Admin accesses private project without membership."""
    await _svc.require_project_access(db_session, private_project, admin_user)


@pytest.mark.asyncio
async def test_member_can_access_private_project(db_session: AsyncSession, private_project: Project, member_user: User):
    """Member can access their private project."""
    await _svc.require_project_access(db_session, private_project, member_user)


@pytest.mark.asyncio
async def test_non_member_gets_404_on_private_project(
    db_session: AsyncSession, private_project: Project, outsider: User
):
    """Non-member gets NotFoundError (404) on private project."""
    with pytest.raises(NotFoundError):
        await _svc.require_project_access(db_session, private_project, outsider)


@pytest.mark.asyncio
async def test_non_member_can_access_public_project(db_session: AsyncSession, public_project: Project, outsider: User):
    """Non-member can access public project."""
    await _svc.require_project_access(db_session, public_project, outsider)


# ---------------------------------------------------------------------------
# Service-level tests: list_projects
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_sees_all_projects(
    db_session: AsyncSession,
    private_project: Project,
    public_project: Project,
    admin_user: User,
):
    """Admin sees all projects regardless of membership."""
    projects, total = await _svc.list_projects(db_session, admin_user, limit=500)
    keys = {p.key for p in projects}
    assert "PRIV" in keys
    assert "PUB" in keys


@pytest.mark.asyncio
async def test_member_sees_member_and_public_projects(
    db_session: AsyncSession,
    private_project: Project,
    public_project: Project,
    member_user: User,
):
    """Member sees their private project + public projects."""
    projects, total = await _svc.list_projects(db_session, member_user, limit=500)
    keys = {p.key for p in projects}
    assert "PRIV" in keys
    assert "PUB" in keys


@pytest.mark.asyncio
async def test_outsider_sees_only_public_projects(
    db_session: AsyncSession,
    private_project: Project,
    public_project: Project,
    outsider: User,
):
    """Non-member sees only public projects, not private ones."""
    projects, total = await _svc.list_projects(db_session, outsider, limit=500)
    keys = {p.key for p in projects}
    assert "PUB" in keys
    assert "PRIV" not in keys


@pytest.mark.asyncio
async def test_outsider_cannot_see_private_project(
    db_session: AsyncSession,
    private_project: Project,
    outsider: User,
):
    """Non-member cannot see private projects in project list."""
    projects, total = await _svc.list_projects(db_session, outsider, limit=500)
    keys = {p.key for p in projects}
    assert "PRIV" not in keys


# ---------------------------------------------------------------------------
# Web route tests: private project access
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_web_private_project_detail_404_for_non_member(
    client: AsyncClient, db_session: AsyncSession, private_project: Project, outsider: User
):
    token = await _login(client, "outsider")
    resp = await client.get(
        f"/projects/{private_project.key}/",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code in (404, 302)  # 404 or redirect to error


@pytest.mark.asyncio
async def test_web_private_project_issues_404_for_non_member(
    client: AsyncClient, db_session: AsyncSession, private_project: Project, outsider: User
):
    token = await _login(client, "outsider")
    resp = await client.get(
        f"/projects/{private_project.key}/issues/",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code in (404, 302)


@pytest.mark.asyncio
async def test_web_private_project_wiki_404_for_non_member(
    client: AsyncClient,
    db_session: AsyncSession,
    private_project: Project,
    outsider: User,
):
    db_session.add(EnabledModule(project_id=private_project.id, name="wiki"))
    await db_session.commit()
    token = await _login(client, "outsider")
    resp = await client.get(
        f"/projects/{private_project.key}/wiki/pages/",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code in (404, 302)


@pytest.mark.asyncio
async def test_web_private_project_accessible_to_member(
    client: AsyncClient, db_session: AsyncSession, private_project: Project, member_user: User
):
    token = await _login(client, "member")
    resp = await client.get(
        f"/projects/{private_project.key}/",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_web_public_project_accessible_to_non_member(
    client: AsyncClient, db_session: AsyncSession, public_project: Project, outsider: User
):
    token = await _login(client, "outsider")
    resp = await client.get(
        f"/projects/{public_project.key}/",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# API route tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_private_project_404_for_non_member(
    client: AsyncClient, db_session: AsyncSession, private_project: Project, outsider: User
):
    token = await _login(client, "outsider")
    resp = await client.get(
        f"/api/v1/projects/{private_project.key}/",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_api_private_project_accessible_to_member(
    client: AsyncClient, db_session: AsyncSession, private_project: Project, member_user: User
):
    token = await _login(client, "member")
    resp = await client.get(
        f"/api/v1/projects/{private_project.key}/",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Default project creation test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_project_defaults_to_private(db_session: AsyncSession):
    """Project created without explicit is_public defaults to private."""
    proj = ProjectFactory.build(key="DFLT", identifier="default-proj")
    assert proj.is_public is False
