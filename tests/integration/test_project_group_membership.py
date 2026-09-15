"""Project membership held by a user group, through the members API.

A ``members`` row is held by exactly one principal — a user or a user group —
so the membership endpoints address a principal rather than a user id. These
tests pin what that buys and what it must never cost:

- a group can be granted roles on a project, and every user in it inherits them;
- the two kinds are listed together but stay distinguishable;
- and an operation aimed at one kind never touches the other's row, which is
  the dangerous failure mode: removing a user must not revoke a group's grant.

Group and role names are unique database-wide and test modules run in parallel
inside uncommitted transactions, so names here carry a module-local prefix.
"""

from __future__ import annotations

import itertools

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.member import Member
from specivo.models.project import Project
from specivo.models.role import Role, RoleBuiltin
from specivo.models.security_audit import SecurityAuditLog
from specivo.models.user import User
from specivo.models.user_group import UserGroup, UserGroupMember
from specivo.services.permission_service import clear_role_cache, get_user_roles
from specivo.services.project_service import Principal, ProjectService
from specivo.services.security_audit_service import AuditEvent, MemberAction
from tests.factories.project import ProjectFactory
from tests.factories.user import UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

_svc = ProjectService()

# Module-local name prefix — see the module docstring.
_PREFIX = "pgmem"
_counter = itertools.count(1)


def _name(stem: str) -> str:
    return f"{_PREFIX}-{stem}-{next(_counter)}"


def _members_url(project: Project) -> str:
    return f"/api/v1/projects/{project.key}/members/"


def _principal_url(project: Project, kind: str, principal_id: int) -> str:
    return f"/api/v1/projects/{project.key}/members/{kind}/{principal_id}/"


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def project(db_session: AsyncSession) -> Project:
    obj = ProjectFactory.build(is_public=True)
    db_session.add(obj)
    await db_session.commit()
    await db_session.refresh(obj)
    return obj


@pytest_asyncio.fixture
async def group(db_session: AsyncSession) -> UserGroup:
    obj = UserGroup(name=_name("Developers"), description="Writes code")
    db_session.add(obj)
    await db_session.commit()
    await db_session.refresh(obj)
    return obj


@pytest_asyncio.fixture
async def role(db_session: AsyncSession) -> Role:
    obj = Role(
        name=_name("Contributor"),
        permissions=["view_issues"],
        builtin=0,
        assignable=True,
        issues_visibility="default",
    )
    db_session.add(obj)
    await db_session.commit()
    await db_session.refresh(obj)
    return obj


@pytest_asyncio.fixture
async def other_role(db_session: AsyncSession) -> Role:
    obj = Role(
        name=_name("Reviewer"),
        permissions=["view_issues"],
        builtin=0,
        assignable=True,
        issues_visibility="default",
    )
    db_session.add(obj)
    await db_session.commit()
    await db_session.refresh(obj)
    return obj


async def _make_user(db: AsyncSession) -> User:
    user = UserFactory.build()
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _join_group(db: AsyncSession, group: UserGroup, user: User) -> None:
    db.add(UserGroupMember(group_id=group.id, user_id=user.id))
    await db.commit()


async def _member_events(db: AsyncSession, project: Project) -> list[SecurityAuditLog]:
    """Return the member_change audit rows for one project, oldest first."""
    result = await db.execute(
        select(SecurityAuditLog)
        .where(
            SecurityAuditLog.event_type == AuditEvent.MEMBER_CHANGE,
            SecurityAuditLog.project_id == project.id,
        )
        .order_by(SecurityAuditLog.created_at, SecurityAuditLog.id)
    )
    return list(result.scalars().all())


async def _member_row_ids(db: AsyncSession, project: Project) -> list[tuple[int | None, int | None]]:
    """Return ``(user_id, group_id)`` for every membership row on *project*."""
    result = await db.execute(
        select(Member.user_id, Member.group_id).where(Member.project_id == project.id).order_by(Member.id)
    )
    return [(row[0], row[1]) for row in result.all()]


# ---------------------------------------------------------------------------
# The API rejects a request that does not name exactly one principal
# ---------------------------------------------------------------------------


class TestPrincipalRequired:
    async def test_api_rejects_both_principals(
        self, admin_client: AsyncClient, project: Project, group: UserGroup, role: Role
    ):
        resp = await admin_client.post(
            _members_url(project),
            json={"user_id": admin_client.state.user.id, "group_id": group.id, "role_ids": [role.id]},
        )
        assert resp.status_code == 422, resp.text

    async def test_api_rejects_neither_principal(self, admin_client: AsyncClient, project: Project, role: Role):
        resp = await admin_client.post(_members_url(project), json={"role_ids": [role.id]})
        assert resp.status_code == 422, resp.text

    async def test_api_rejects_an_unknown_principal_kind(self, admin_client: AsyncClient, project: Project, role: Role):
        resp = await admin_client.delete(_principal_url(project, "groups", 1))
        assert resp.status_code == 422, resp.text
        assert "principal_type" in resp.text


# ---------------------------------------------------------------------------
# Adding a group
# ---------------------------------------------------------------------------


class TestAddGroup:
    async def test_add_group_returns_a_group_row(
        self, admin_client: AsyncClient, db_session: AsyncSession, project: Project, group: UserGroup, role: Role
    ):
        member_user = await _make_user(db_session)
        await _join_group(db_session, group, member_user)

        resp = await admin_client.post(
            _members_url(project),
            json={"group_id": group.id, "role_ids": [role.id]},
        )

        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["principal_type"] == "group"
        assert body["group_id"] == group.id
        assert body["name"] == group.name
        assert body["user_count"] == 1
        assert body["roles"] == [role.name]
        assert body["role_ids"] == [role.id]
        assert body["user_id"] is None
        assert body["login"] is None

    async def test_group_members_resolve_the_groups_roles(
        self, admin_client: AsyncClient, db_session: AsyncSession, project: Project, group: UserGroup, role: Role
    ):
        """The user holds no membership row of their own — only the group does."""
        member_user = await _make_user(db_session)
        await _join_group(db_session, group, member_user)

        resp = await admin_client.post(
            _members_url(project),
            json={"group_id": group.id, "role_ids": [role.id]},
        )
        assert resp.status_code == 201, resp.text

        clear_role_cache(db_session)
        roles = await get_user_roles(db_session, member_user, project)
        assert [r.name for r in roles] == [role.name]

    async def test_a_user_outside_the_group_resolves_nothing(
        self, admin_client: AsyncClient, db_session: AsyncSession, project: Project, group: UserGroup, role: Role
    ):
        outsider = await _make_user(db_session)

        await admin_client.post(_members_url(project), json={"group_id": group.id, "role_ids": [role.id]})

        clear_role_cache(db_session)
        # The project is public, so an outsider falls back to the Non member role only.
        roles = await get_user_roles(db_session, outsider, project)
        assert [r.builtin for r in roles] == [RoleBuiltin.NON_MEMBER]

    async def test_add_group_twice_adds_roles_to_the_same_row(
        self,
        admin_client: AsyncClient,
        db_session: AsyncSession,
        project: Project,
        group: UserGroup,
        role: Role,
        other_role: Role,
    ):
        await admin_client.post(_members_url(project), json={"group_id": group.id, "role_ids": [role.id]})
        resp = await admin_client.post(_members_url(project), json={"group_id": group.id, "role_ids": [other_role.id]})

        assert resp.status_code == 201, resp.text
        assert sorted(resp.json()["roles"]) == sorted([role.name, other_role.name])
        assert await _member_row_ids(db_session, project) == [(None, group.id)]

    async def test_add_unknown_group_is_404(self, admin_client: AsyncClient, project: Project, role: Role):
        resp = await admin_client.post(_members_url(project), json={"group_id": 10_000_000, "role_ids": [role.id]})
        assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# Listing both kinds
# ---------------------------------------------------------------------------


class TestListMembers:
    async def test_list_returns_user_rows_and_group_rows_discriminated(
        self, admin_client: AsyncClient, db_session: AsyncSession, project: Project, group: UserGroup, role: Role
    ):
        direct_user = await _make_user(db_session)
        grouped_user = await _make_user(db_session)
        await _join_group(db_session, group, grouped_user)

        await admin_client.post(_members_url(project), json={"user_id": direct_user.id, "role_ids": [role.id]})
        await admin_client.post(_members_url(project), json={"group_id": group.id, "role_ids": [role.id]})

        resp = await admin_client.get(_members_url(project))

        assert resp.status_code == 200, resp.text
        rows = resp.json()
        by_kind = {row["principal_type"]: row for row in rows}
        assert set(by_kind) == {"user", "group"}

        user_row = by_kind["user"]
        assert user_row["user_id"] == direct_user.id
        assert user_row["login"] == direct_user.login
        assert user_row["group_id"] is None
        assert user_row["user_count"] is None

        group_row = by_kind["group"]
        assert group_row["group_id"] == group.id
        assert group_row["name"] == group.name
        assert group_row["user_count"] == 1
        assert group_row["user_id"] is None

    async def test_service_list_members_still_returns_only_user_rows(
        self, admin_client: AsyncClient, db_session: AsyncSession, project: Project, group: UserGroup, role: Role
    ):
        """The assignee pickers call this; a group must never reach them."""
        direct_user = await _make_user(db_session)
        await admin_client.post(_members_url(project), json={"user_id": direct_user.id, "role_ids": [role.id]})
        await admin_client.post(_members_url(project), json={"group_id": group.id, "role_ids": [role.id]})

        rows = await _svc.list_members(db_session, project)

        assert [row["user_id"] for row in rows] == [direct_user.id]
        assert {row["principal_type"] for row in rows} == {"user"}

    async def test_service_list_group_memberships_returns_only_group_rows(
        self, admin_client: AsyncClient, db_session: AsyncSession, project: Project, group: UserGroup, role: Role
    ):
        direct_user = await _make_user(db_session)
        await _join_group(db_session, group, direct_user)
        await admin_client.post(_members_url(project), json={"user_id": direct_user.id, "role_ids": [role.id]})
        await admin_client.post(_members_url(project), json={"group_id": group.id, "role_ids": [role.id]})

        rows = await _svc.list_group_memberships(db_session, project)

        assert rows == [
            {
                "principal_type": "group",
                "group_id": group.id,
                "name": group.name,
                "user_count": 1,
                "roles": [role.name],
                "role_ids": [role.id],
            }
        ]


# ---------------------------------------------------------------------------
# Updating and removing a group membership
# ---------------------------------------------------------------------------


class TestUpdateAndRemoveGroup:
    async def test_patch_replaces_the_groups_roles(
        self,
        admin_client: AsyncClient,
        db_session: AsyncSession,
        project: Project,
        group: UserGroup,
        role: Role,
        other_role: Role,
    ):
        member_user = await _make_user(db_session)
        await _join_group(db_session, group, member_user)
        await admin_client.post(_members_url(project), json={"group_id": group.id, "role_ids": [role.id]})

        resp = await admin_client.patch(_principal_url(project, "group", group.id), json={"role_ids": [other_role.id]})

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["principal_type"] == "group"
        assert body["roles"] == [other_role.name]

        clear_role_cache(db_session)
        roles = await get_user_roles(db_session, member_user, project)
        assert [r.name for r in roles] == [other_role.name]

    async def test_delete_removes_the_groups_membership(
        self, admin_client: AsyncClient, db_session: AsyncSession, project: Project, group: UserGroup, role: Role
    ):
        member_user = await _make_user(db_session)
        await _join_group(db_session, group, member_user)
        await admin_client.post(_members_url(project), json={"group_id": group.id, "role_ids": [role.id]})

        resp = await admin_client.delete(_principal_url(project, "group", group.id))

        assert resp.status_code == 204, resp.text
        assert await _member_row_ids(db_session, project) == []

        clear_role_cache(db_session)
        # Without the group's row the user is a non-member of this public project.
        roles = await get_user_roles(db_session, member_user, project)
        assert [r.builtin for r in roles] == [RoleBuiltin.NON_MEMBER]

    async def test_patch_on_a_group_that_is_not_a_member_is_404(
        self, admin_client: AsyncClient, project: Project, group: UserGroup, role: Role
    ):
        resp = await admin_client.patch(_principal_url(project, "group", group.id), json={"role_ids": [role.id]})
        assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# Principal kinds never match each other
# ---------------------------------------------------------------------------


class TestPrincipalIsolation:
    """A user id and a group id are different namespaces, and they collide.

    Nothing stops user 3 and group 3 both being members of one project, so an
    operation that addressed rows by bare id would revoke the wrong grant.
    These tests force the collision: the ids are made equal wherever the
    fixtures allow, and every case still has to touch exactly one row.
    """

    async def test_removing_a_user_leaves_the_group_membership(
        self, admin_client: AsyncClient, db_session: AsyncSession, project: Project, group: UserGroup, role: Role
    ):
        direct_user = await _make_user(db_session)
        await admin_client.post(_members_url(project), json={"user_id": direct_user.id, "role_ids": [role.id]})
        await admin_client.post(_members_url(project), json={"group_id": group.id, "role_ids": [role.id]})

        resp = await admin_client.delete(_principal_url(project, "user", direct_user.id))

        assert resp.status_code == 204, resp.text
        assert await _member_row_ids(db_session, project) == [(None, group.id)]

    async def test_removing_a_group_leaves_the_user_membership(
        self, admin_client: AsyncClient, db_session: AsyncSession, project: Project, group: UserGroup, role: Role
    ):
        direct_user = await _make_user(db_session)
        await admin_client.post(_members_url(project), json={"user_id": direct_user.id, "role_ids": [role.id]})
        await admin_client.post(_members_url(project), json={"group_id": group.id, "role_ids": [role.id]})

        resp = await admin_client.delete(_principal_url(project, "group", group.id))

        assert resp.status_code == 204, resp.text
        assert await _member_row_ids(db_session, project) == [(direct_user.id, None)]

    async def test_removing_a_user_by_the_groups_id_is_404(
        self, admin_client: AsyncClient, db_session: AsyncSession, project: Project, group: UserGroup, role: Role
    ):
        """Only the group holds a row; addressing it as a user must not find it."""
        await admin_client.post(_members_url(project), json={"group_id": group.id, "role_ids": [role.id]})

        resp = await admin_client.delete(_principal_url(project, "user", group.id))

        assert resp.status_code == 404, resp.text
        assert await _member_row_ids(db_session, project) == [(None, group.id)]

    async def test_removing_a_group_by_the_users_id_is_404(
        self, admin_client: AsyncClient, db_session: AsyncSession, project: Project, role: Role
    ):
        direct_user = await _make_user(db_session)
        await admin_client.post(_members_url(project), json={"user_id": direct_user.id, "role_ids": [role.id]})

        resp = await admin_client.delete(_principal_url(project, "group", direct_user.id))

        assert resp.status_code == 404, resp.text
        assert await _member_row_ids(db_session, project) == [(direct_user.id, None)]

    async def test_patching_a_group_does_not_touch_the_users_roles(
        self,
        admin_client: AsyncClient,
        db_session: AsyncSession,
        project: Project,
        group: UserGroup,
        role: Role,
        other_role: Role,
    ):
        direct_user = await _make_user(db_session)
        await admin_client.post(_members_url(project), json={"user_id": direct_user.id, "role_ids": [role.id]})
        await admin_client.post(_members_url(project), json={"group_id": group.id, "role_ids": [role.id]})

        resp = await admin_client.patch(_principal_url(project, "group", group.id), json={"role_ids": [other_role.id]})
        assert resp.status_code == 200, resp.text

        user_rows = await _svc.list_members(db_session, project)
        assert [row["roles"] for row in user_rows] == [[role.name]]

    async def test_service_never_matches_across_principal_kinds(
        self, db_session: AsyncSession, project: Project, group: UserGroup, role: Role
    ):
        """The service layer's own guard, independent of the routing."""
        await _svc.add_member(db_session, project, Principal.group(group.id), [role.id])
        await db_session.flush()

        found = await _svc._find_member_row(db_session, project, Principal.user(group.id))

        assert found is None


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------


class TestPermissions:
    async def test_non_manager_cannot_add_a_group(
        self, auth_client: AsyncClient, project: Project, group: UserGroup, role: Role
    ):
        resp = await auth_client.post(_members_url(project), json={"group_id": group.id, "role_ids": [role.id]})
        assert resp.status_code == 403, resp.text

    async def test_non_manager_cannot_remove_a_group(
        self, admin_client: AsyncClient, auth_client: AsyncClient, project: Project, group: UserGroup, role: Role
    ):
        await admin_client.post(_members_url(project), json={"group_id": group.id, "role_ids": [role.id]})

        resp = await auth_client.delete(_principal_url(project, "group", group.id))

        assert resp.status_code == 403, resp.text

    async def test_non_manager_cannot_change_a_groups_roles(
        self,
        admin_client: AsyncClient,
        auth_client: AsyncClient,
        project: Project,
        group: UserGroup,
        role: Role,
        other_role: Role,
    ):
        await admin_client.post(_members_url(project), json={"group_id": group.id, "role_ids": [role.id]})

        resp = await auth_client.patch(_principal_url(project, "group", group.id), json={"role_ids": [other_role.id]})

        assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


class TestAudit:
    async def test_granting_a_group_writes_an_audit_row_naming_project_and_group(
        self, admin_client: AsyncClient, db_session: AsyncSession, project: Project, group: UserGroup, role: Role
    ):
        await admin_client.post(_members_url(project), json={"group_id": group.id, "role_ids": [role.id]})

        events = await _member_events(db_session, project)

        assert len(events) == 1
        event = events[0]
        assert event.project_id == project.id
        assert event.details["action"] == MemberAction.ADDED
        assert event.details["principal_type"] == "group"
        assert event.details["target_group_id"] == group.id
        assert event.details["target_group_name"] == group.name
        assert event.details["roles"] == [role.name]

    async def test_revoking_a_group_writes_an_audit_row_naming_project_and_group(
        self, admin_client: AsyncClient, db_session: AsyncSession, project: Project, group: UserGroup, role: Role
    ):
        await admin_client.post(_members_url(project), json={"group_id": group.id, "role_ids": [role.id]})

        await admin_client.delete(_principal_url(project, "group", group.id))

        events = await _member_events(db_session, project)
        assert [e.details["action"] for e in events] == [MemberAction.ADDED, MemberAction.REMOVED]
        removed = events[-1]
        assert removed.project_id == project.id
        assert removed.details["principal_type"] == "group"
        assert removed.details["target_group_id"] == group.id
        assert removed.details["target_group_name"] == group.name

    async def test_a_user_grant_is_still_recorded_as_a_user(
        self, admin_client: AsyncClient, db_session: AsyncSession, project: Project, role: Role
    ):
        direct_user = await _make_user(db_session)

        await admin_client.post(_members_url(project), json={"user_id": direct_user.id, "role_ids": [role.id]})

        events = await _member_events(db_session, project)
        assert len(events) == 1
        assert events[0].details["principal_type"] == "user"
        assert events[0].details["target_user_id"] == direct_user.id
        assert events[0].details["target_login"] == direct_user.login


# ---------------------------------------------------------------------------
# MCP
# ---------------------------------------------------------------------------


class TestMcpListMembers:
    """The MCP tool is an assignee picker too — it must not learn about groups.

    ``_list_members`` is called directly rather than through the MCP server:
    the tool is a plain function of (session, user, project_key) and needs
    none of the server's global session state, so it can run in the ordinary
    parallel pass.
    """

    async def test_mcp_output_lists_only_users(
        self, admin_client: AsyncClient, db_session: AsyncSession, project: Project, group: UserGroup, role: Role
    ):
        from specivo.mcp.tools import _list_members

        direct_user = await _make_user(db_session)
        grouped_user = await _make_user(db_session)
        await _join_group(db_session, group, grouped_user)
        await admin_client.post(_members_url(project), json={"user_id": direct_user.id, "role_ids": [role.id]})
        await admin_client.post(_members_url(project), json={"group_id": group.id, "role_ids": [role.id]})

        clear_role_cache(db_session)
        out = await _list_members(db_session, direct_user, project.key)

        assert f"Members of {project.key} (1 total):" in out
        assert f"{direct_user.id}  {direct_user.login}  —  {direct_user.display_name}  [{role.name}]" in out
        assert group.name not in out
        assert str(grouped_user.login) not in out
