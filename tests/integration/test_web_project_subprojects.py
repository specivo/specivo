"""Subprojects listed on a project's overview page, per signed-in visitor.

The overview names each child project by name and key. A child is listed only
when the visitor could open it: the same rule the project list applies, so a
private child the visitor holds no membership on is not disclosed through its
parent's page. Admins reach every project and see every child.

The anonymous half of this rule is pinned in ``test_anonymous_web_access.py``.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.role import Role
from specivo.models.user import User
from tests.factories.project import ProjectFactory
from tests.factories.user import TEST_PASSWORD, AdminUserFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

# Names and keys that appear nowhere else on the page, so a match is a leak.
PRIVATE_CHILD_NAME = "Heron Ledger Archive"
PRIVATE_CHILD_KEY = "HERONLDG"
PUBLIC_CHILD_NAME = "Kestrel Field Notes"
PUBLIC_CHILD_KEY = "KESTRELF"


@dataclass(frozen=True)
class Tree:
    """A private parent with one private and one public child."""

    parent: Project
    private_child: Project
    public_child: Project
    parent_only: User
    both: User
    admin: User


async def _make_project(
    db: AsyncSession,
    *,
    name: str,
    key: str,
    identifier: str,
    is_public: bool,
    parent: Project | None = None,
) -> Project:
    path = identifier.replace("-", "_")
    project = ProjectFactory.build(
        name=name,
        key=key,
        identifier=identifier,
        is_public=is_public,
        parent_id=parent.id if parent is not None else None,
        path=f"{parent.path}.{path}" if parent is not None else path,
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


async def _add_member(db: AsyncSession, project: Project, user: User) -> None:
    role = Role(
        name=f"Role-{project.key}-{user.id}",
        permissions=["*"],
        builtin=0,
        issues_visibility="default",
    )
    db.add(role)
    await db.flush()
    member = Member(user_id=user.id, project_id=project.id)
    db.add(member)
    await db.flush()
    db.add(MemberRole(member_id=member.id, role_id=role.id))
    await db.commit()


@pytest_asyncio.fixture
async def tree(db_session: AsyncSession) -> Tree:
    parent = await _make_project(
        db_session, name="Osprey Parent", key="OSPREYP", identifier="osprey-parent", is_public=False
    )
    private_child = await _make_project(
        db_session,
        name=PRIVATE_CHILD_NAME,
        key=PRIVATE_CHILD_KEY,
        identifier="heron-ledger",
        is_public=False,
        parent=parent,
    )
    public_child = await _make_project(
        db_session,
        name=PUBLIC_CHILD_NAME,
        key=PUBLIC_CHILD_KEY,
        identifier="kestrel-field",
        is_public=True,
        parent=parent,
    )

    users = {}
    for login in ("subproj_parent_only", "subproj_both"):
        user = UserFactory.build(login=login, status="active")
        db_session.add(user)
        users[login] = user
    admin = AdminUserFactory.build(login="subproj_admin", status="active")
    db_session.add(admin)
    await db_session.commit()

    parent_only = users["subproj_parent_only"]
    both = users["subproj_both"]
    await _add_member(db_session, parent, parent_only)
    await _add_member(db_session, parent, both)
    await _add_member(db_session, private_child, both)

    return Tree(
        parent=parent,
        private_child=private_child,
        public_child=public_child,
        parent_only=parent_only,
        both=both,
        admin=admin,
    )


async def _overview(client: AsyncClient, login: str, key: str) -> str:
    resp = await client.post("/api/v1/auth/login/", json={"login": login, "password": TEST_PASSWORD})
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]
    page = await client.get(f"/projects/{key}/", cookies={"access_token": token})
    assert page.status_code == 200, page.text
    return page.text


async def test_parent_member_does_not_see_private_child(client: AsyncClient, tree: Tree) -> None:
    """A member of the parent only is not told the private child exists."""
    html = await _overview(client, tree.parent_only.login, tree.parent.key)

    assert PRIVATE_CHILD_NAME not in html
    assert PRIVATE_CHILD_KEY not in html
    assert f"/projects/{PRIVATE_CHILD_KEY}/" not in html


async def test_parent_member_still_sees_public_child(client: AsyncClient, tree: Tree) -> None:
    """A public child is one every signed-in user can open, so it stays listed."""
    html = await _overview(client, tree.parent_only.login, tree.parent.key)

    assert PUBLIC_CHILD_NAME in html
    assert f"/projects/{PUBLIC_CHILD_KEY}/" in html


async def test_member_of_both_sees_private_child(client: AsyncClient, tree: Tree) -> None:
    html = await _overview(client, tree.both.login, tree.parent.key)

    assert PRIVATE_CHILD_NAME in html
    assert f"/projects/{PRIVATE_CHILD_KEY}/" in html
    assert PUBLIC_CHILD_NAME in html


async def test_admin_sees_private_child(client: AsyncClient, tree: Tree) -> None:
    html = await _overview(client, tree.admin.login, tree.parent.key)

    assert PRIVATE_CHILD_NAME in html
    assert f"/projects/{PRIVATE_CHILD_KEY}/" in html
    assert PUBLIC_CHILD_NAME in html
