"""Web admin user-group page tests.

Covers the two pages behind ``/admin/groups/``: the list and one group's
detail. Both are admin-only, and the detail page's reason to exist is that it
names the projects a group grants access to — so the tests assert that the
access shows up on the page, not merely that the page returns 200.

Group and role names are unique database-wide and test modules run in
parallel inside uncommitted transactions, so names here carry a module-local
prefix.
"""

from __future__ import annotations

import itertools
from html.parser import HTMLParser

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.role import Role
from specivo.models.user import User
from specivo.models.user_group import UserGroup, UserGroupMember
from tests.factories.project import ProjectFactory
from tests.factories.user import UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

# Module-local name prefix — see the module docstring.
_PREFIX = "webgrp"
_counter = itertools.count(1)


def _name(stem: str) -> str:
    return f"{_PREFIX}-{stem}-{next(_counter)}"


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def group(db_session: AsyncSession) -> UserGroup:
    obj = UserGroup(name=_name("Developers"), description="Writes the code")
    db_session.add(obj)
    await db_session.commit()
    await db_session.refresh(obj)
    return obj


async def _make_user(db: AsyncSession) -> User:
    obj = UserFactory.build()
    db.add(obj)
    await db.flush()
    return obj


async def _make_project(db: AsyncSession) -> Project:
    obj = ProjectFactory.build()
    db.add(obj)
    await db.flush()
    return obj


async def _make_role(db: AsyncSession, stem: str) -> Role:
    role = Role(name=_name(stem), permissions=["view_issues"], builtin=0, issues_visibility="default")
    db.add(role)
    await db.flush()
    return role


async def _grant(db: AsyncSession, project: Project, group: UserGroup, role: Role) -> None:
    """Give *group* a membership on *project* holding *role*."""
    member = Member(project_id=project.id, group_id=group.id)
    db.add(member)
    await db.flush()
    db.add(MemberRole(member_id=member.id, role_id=role.id))
    await db.commit()


# ---------------------------------------------------------------------------
# List page
# ---------------------------------------------------------------------------


async def test_groups_page_renders_for_admin(admin_client: AsyncClient, group: UserGroup):
    resp = await admin_client.get("/admin/groups/", cookies={"access_token": admin_client.state.token})

    assert resp.status_code == 200
    assert group.name in resp.text


async def test_groups_page_forbidden_for_non_admin(auth_client: AsyncClient):
    resp = await auth_client.get("/admin/groups/", cookies={"access_token": auth_client.state.token})

    assert resp.status_code == 403


async def test_groups_page_carries_the_counts_the_delete_warning_needs(
    admin_client: AsyncClient, db_session: AsyncSession, group: UserGroup
):
    """The blast-radius modal reads its numbers off the row, so the row must carry them."""
    user = await _make_user(db_session)
    db_session.add(UserGroupMember(group_id=group.id, user_id=user.id))
    project = await _make_project(db_session)
    await _grant(db_session, project, group, await _make_role(db_session, "Viewer"))

    resp = await admin_client.get("/admin/groups/", cookies={"access_token": admin_client.state.token})

    assert resp.status_code == 200
    assert '"user_count": 1' in resp.text
    assert '"project_count": 1' in resp.text


# ---------------------------------------------------------------------------
# Detail page
# ---------------------------------------------------------------------------


async def test_group_detail_lists_users_and_granted_projects(
    admin_client: AsyncClient, db_session: AsyncSession, group: UserGroup
):
    user = await _make_user(db_session)
    db_session.add(UserGroupMember(group_id=group.id, user_id=user.id))
    project = await _make_project(db_session)
    role = await _make_role(db_session, "Viewer")
    await _grant(db_session, project, group, role)

    resp = await admin_client.get(
        f"/admin/groups/{group.id}/",
        cookies={"access_token": admin_client.state.token},
    )

    assert resp.status_code == 200
    assert user.login in resp.text
    # The point of the page: the access the group grants is named on it.
    assert project.name in resp.text
    assert role.name in resp.text


async def test_group_detail_forbidden_for_non_admin(auth_client: AsyncClient, group: UserGroup):
    resp = await auth_client.get(
        f"/admin/groups/{group.id}/",
        cookies={"access_token": auth_client.state.token},
    )

    assert resp.status_code == 403


async def test_group_detail_404_for_unknown_group(admin_client: AsyncClient):
    resp = await admin_client.get("/admin/groups/999999/", cookies={"access_token": admin_client.state.token})

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Dashboard entry point
# ---------------------------------------------------------------------------


async def test_admin_dashboard_links_to_groups(admin_client: AsyncClient):
    resp = await admin_client.get("/admin/", cookies={"access_token": admin_client.state.token})

    assert resp.status_code == 200
    assert "/admin/groups/" in resp.text


class _XDataGrabber(HTMLParser):
    """Collects the value of every ``x-data`` attribute naming *component*."""

    def __init__(self, component: str) -> None:
        super().__init__()
        self._component = component
        self.values: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name == "x-data" and value and self._component in value:
                self.values.append(value)


def _x_data(html: str, component: str) -> str:
    grabber = _XDataGrabber(component)
    grabber.feed(html)
    assert grabber.values, f"no x-data attribute for {component!r} in the rendered page"
    return grabber.values[0]


async def test_group_pages_x_data_attributes_survive_html_parsing(admin_client: AsyncClient, group: UserGroup):
    """ADR-0001 section 3: a truncated x-data leaves Alpine failing silently.

    Both pages hand the component server data and translated strings through
    ``tojson`` inside a single-quoted attribute. Parsing the response as HTML
    and looking for the object's last key proves nothing closed the attribute
    early.
    """
    resp = await admin_client.get("/admin/groups/", cookies={"access_token": admin_client.state.token})
    attr = _x_data(resp.text, "adminGroups(")
    assert "groups:" in attr
    assert "deleteFailed:" in attr
    assert attr.rstrip().endswith("})")

    resp = await admin_client.get(
        f"/admin/groups/{group.id}/",
        cookies={"access_token": admin_client.state.token},
    )
    attr = _x_data(resp.text, "adminGroupDetail(")
    for key in ("groupId:", "groupName:", "users:", "confirmRemove:"):
        assert key in attr, f"{key} missing from the x-data attribute — it was truncated"
    assert attr.rstrip().endswith("})")
