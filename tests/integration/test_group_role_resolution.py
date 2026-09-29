"""Role and access resolution through user-group memberships.

A ``members`` row is held by exactly one principal — a user or a user group —
and both kinds carry roles the same way. These tests pin the consequence: a
user reaches a project, and holds its roles, either directly or through any
group they belong to, and loses that reach the moment the link is broken.
"""

from __future__ import annotations

import itertools

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import NotFoundError
from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.role import Role
from specivo.models.user import User
from specivo.models.user_group import UserGroup, UserGroupMember
from specivo.services.permission_service import (
    Permission,
    check_permission,
    clear_role_cache,
    get_user_roles,
)
from specivo.services.project_service import ProjectService
from tests.factories.project import ProjectFactory
from tests.factories.user import UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

_svc = ProjectService()


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def user(db_session: AsyncSession) -> User:
    obj = UserFactory.build()
    db_session.add(obj)
    await db_session.flush()
    return obj


@pytest_asyncio.fixture
async def project(db_session: AsyncSession) -> Project:
    obj = ProjectFactory.build(is_public=False)
    db_session.add(obj)
    await db_session.flush()
    return obj


# Group and role names are unique DB-wide, so every helper-built name gets a
# suffix from this counter.
_names_counter = itertools.count(1)


async def _make_group(db: AsyncSession, name: str) -> UserGroup:
    group = UserGroup(name=f"{name}-{next(_names_counter)}")
    db.add(group)
    await db.flush()
    return group


async def _make_role(db: AsyncSession, name: str, permissions: list[str] | None = None) -> Role:
    role = Role(
        name=f"{name}-{next(_names_counter)}",
        permissions=permissions if permissions is not None else [Permission.VIEW_ISSUES],
        builtin=0,
        issues_visibility="default",
    )
    db.add(role)
    await db.flush()
    return role


async def _add_principal(
    db: AsyncSession,
    project: Project,
    *,
    user: User | None = None,
    group: UserGroup | None = None,
    roles: list[Role],
) -> Member:
    """Add a user or a group to *project* holding *roles*."""
    member = Member(
        user_id=user.id if user is not None else None,
        group_id=group.id if group is not None else None,
        project_id=project.id,
    )
    db.add(member)
    await db.flush()
    for role in roles:
        db.add(MemberRole(member_id=member.id, role_id=role.id))
    await db.flush()
    return member


async def _join_group(db: AsyncSession, group: UserGroup, user: User) -> UserGroupMember:
    link = UserGroupMember(group_id=group.id, user_id=user.id)
    db.add(link)
    await db.flush()
    return link


def _names(roles: list[Role]) -> set[str]:
    return {r.name for r in roles}


# ---------------------------------------------------------------------------
# get_user_roles()
# ---------------------------------------------------------------------------


class TestRoleResolution:
    async def test_direct_membership_resolves_roles(self, db_session: AsyncSession, user: User, project: Project):
        role = await _make_role(db_session, "Direct")
        await _add_principal(db_session, project, user=user, roles=[role])

        roles = await get_user_roles(db_session, user, project)

        assert _names(roles) == {role.name}

    async def test_group_membership_resolves_roles(self, db_session: AsyncSession, user: User, project: Project):
        """The user holds no membership row of their own — only the group does."""
        group = await _make_group(db_session, "Developers")
        await _join_group(db_session, group, user)
        role = await _make_role(db_session, "ViaGroup")
        await _add_principal(db_session, project, group=group, roles=[role])

        roles = await get_user_roles(db_session, user, project)

        assert _names(roles) == {role.name}

    async def test_direct_and_group_roles_are_unioned(self, db_session: AsyncSession, user: User, project: Project):
        group = await _make_group(db_session, "Reviewers")
        await _join_group(db_session, group, user)
        direct_role = await _make_role(db_session, "Direct")
        group_role = await _make_role(db_session, "ViaGroup")
        await _add_principal(db_session, project, user=user, roles=[direct_role])
        await _add_principal(db_session, project, group=group, roles=[group_role])

        roles = await get_user_roles(db_session, user, project)

        assert _names(roles) == {direct_role.name, group_role.name}

    async def test_same_role_through_two_groups_is_returned_once(
        self, db_session: AsyncSession, user: User, project: Project
    ):
        shared_role = await _make_role(db_session, "Developer")
        for name in ("Backend", "Frontend"):
            group = await _make_group(db_session, name)
            await _join_group(db_session, group, user)
            await _add_principal(db_session, project, group=group, roles=[shared_role])

        roles = await get_user_roles(db_session, user, project)

        assert [r.name for r in roles] == [shared_role.name]

    async def test_roles_of_a_group_the_user_is_not_in_are_not_granted(
        self, db_session: AsyncSession, user: User, project: Project
    ):
        group = await _make_group(db_session, "Strangers")
        role = await _make_role(db_session, "NotMine")
        await _add_principal(db_session, project, group=group, roles=[role])

        assert await get_user_roles(db_session, user, project) == []

    async def test_group_roles_are_scoped_to_the_group_s_project(
        self, db_session: AsyncSession, user: User, project: Project
    ):
        """A group's roles apply only to the projects that group is a member of."""
        other = ProjectFactory.build(is_public=False)
        db_session.add(other)
        await db_session.flush()

        group = await _make_group(db_session, "Developers")
        await _join_group(db_session, group, user)
        role = await _make_role(db_session, "ViaGroup")
        await _add_principal(db_session, project, group=group, roles=[role])

        assert await get_user_roles(db_session, user, other) == []

    async def test_leaving_the_group_revokes_roles(self, db_session: AsyncSession, user: User, project: Project):
        group = await _make_group(db_session, "Developers")
        link = await _join_group(db_session, group, user)
        role = await _make_role(db_session, "ViaGroup")
        await _add_principal(db_session, project, group=group, roles=[role])
        assert await get_user_roles(db_session, user, project) != []

        await db_session.execute(text("DELETE FROM user_group_members WHERE id = :id"), {"id": link.id})
        clear_role_cache(db_session)

        assert await get_user_roles(db_session, user, project) == []

    async def test_deleting_the_group_revokes_roles(self, db_session: AsyncSession, user: User, project: Project):
        group = await _make_group(db_session, "Developers")
        await _join_group(db_session, group, user)
        role = await _make_role(db_session, "ViaGroup")
        await _add_principal(db_session, project, group=group, roles=[role])
        assert await get_user_roles(db_session, user, project) != []

        await db_session.execute(text("DELETE FROM user_groups WHERE id = :id"), {"id": group.id})
        clear_role_cache(db_session)

        assert await get_user_roles(db_session, user, project) == []

    async def test_deleting_the_group_leaves_the_direct_membership_intact(
        self, db_session: AsyncSession, user: User, project: Project
    ):
        group = await _make_group(db_session, "Developers")
        await _join_group(db_session, group, user)
        direct_role = await _make_role(db_session, "Direct")
        group_role = await _make_role(db_session, "ViaGroup")
        await _add_principal(db_session, project, user=user, roles=[direct_role])
        await _add_principal(db_session, project, group=group, roles=[group_role])

        await db_session.execute(text("DELETE FROM user_groups WHERE id = :id"), {"id": group.id})
        clear_role_cache(db_session)

        roles = await get_user_roles(db_session, user, project)
        assert _names(roles) == {direct_role.name}


# ---------------------------------------------------------------------------
# check_permission()
# ---------------------------------------------------------------------------


class TestPermissionCheck:
    async def test_group_role_grants_permission(self, db_session: AsyncSession, user: User, project: Project):
        group = await _make_group(db_session, "Developers")
        await _join_group(db_session, group, user)
        role = await _make_role(db_session, "ViaGroup", [Permission.ADD_ISSUES])
        await _add_principal(db_session, project, group=group, roles=[role])

        assert await check_permission(user, project.id, Permission.ADD_ISSUES, db_session) is True
        assert await check_permission(user, project.id, Permission.MANAGE_PROJECT, db_session) is False

    async def test_no_group_no_permission(self, db_session: AsyncSession, user: User, project: Project):
        group = await _make_group(db_session, "Developers")
        role = await _make_role(db_session, "ViaGroup", [Permission.ADD_ISSUES])
        await _add_principal(db_session, project, group=group, roles=[role])

        assert await check_permission(user, project.id, Permission.ADD_ISSUES, db_session) is False


# ---------------------------------------------------------------------------
# Project visibility
# ---------------------------------------------------------------------------


class TestProjectAccess:
    async def test_private_project_is_reachable_through_a_group(
        self, db_session: AsyncSession, user: User, project: Project
    ):
        group = await _make_group(db_session, "Developers")
        await _join_group(db_session, group, user)
        await _add_principal(db_session, project, group=group, roles=[await _make_role(db_session, "ViaGroup")])

        await _svc.require_project_access(db_session, project, user)  # must not raise

    async def test_private_project_is_refused_without_the_group(
        self, db_session: AsyncSession, user: User, project: Project
    ):
        group = await _make_group(db_session, "Developers")
        await _add_principal(db_session, project, group=group, roles=[await _make_role(db_session, "ViaGroup")])

        with pytest.raises(NotFoundError):
            await _svc.require_project_access(db_session, project, user)

    async def test_leaving_the_group_closes_the_project(self, db_session: AsyncSession, user: User, project: Project):
        group = await _make_group(db_session, "Developers")
        link = await _join_group(db_session, group, user)
        await _add_principal(db_session, project, group=group, roles=[await _make_role(db_session, "ViaGroup")])

        await db_session.execute(text("DELETE FROM user_group_members WHERE id = :id"), {"id": link.id})

        with pytest.raises(NotFoundError):
            await _svc.require_project_access(db_session, project, user)

    async def test_list_projects_includes_a_project_reached_only_through_a_group(
        self, db_session: AsyncSession, user: User, project: Project
    ):
        group = await _make_group(db_session, "Developers")
        await _join_group(db_session, group, user)
        await _add_principal(db_session, project, group=group, roles=[await _make_role(db_session, "ViaGroup")])

        projects, total = await _svc.list_projects(db_session, user, limit=100)

        assert project.id in {p.id for p in projects}
        assert total >= 1

    async def test_list_projects_excludes_a_private_project_of_a_group_the_user_is_not_in(
        self, db_session: AsyncSession, user: User, project: Project
    ):
        group = await _make_group(db_session, "Developers")
        await _add_principal(db_session, project, group=group, roles=[await _make_role(db_session, "ViaGroup")])

        projects, _ = await _svc.list_projects(db_session, user, limit=100)

        assert project.id not in {p.id for p in projects}


# ---------------------------------------------------------------------------
# Members listing
# ---------------------------------------------------------------------------


class TestMemberListing:
    async def test_a_group_is_one_membership_row_but_reaches_everyone_in_it(
        self, db_session: AsyncSession, user: User, project: Project
    ):
        """The two counts diverge here, and each screen must use the one it means."""
        group = await _make_group(db_session, "Developers")
        other = UserFactory.build()
        db_session.add(other)
        await db_session.flush()
        await _join_group(db_session, group, user)
        await _join_group(db_session, group, other)
        await _add_principal(db_session, project, group=group, roles=[await _make_role(db_session, "ViaGroup")])

        assert await _svc.count_membership_rows(db_session, project) == 1
        assert await _svc.count_people_with_access(db_session, project) == 2

    async def test_list_members_returns_user_rows_and_skips_group_rows(
        self, db_session: AsyncSession, user: User, project: Project
    ):
        group = await _make_group(db_session, "Developers")
        await _join_group(db_session, group, user)
        direct_user = UserFactory.build()
        db_session.add(direct_user)
        await db_session.flush()
        await _add_principal(db_session, project, group=group, roles=[await _make_role(db_session, "ViaGroup")])
        await _add_principal(db_session, project, user=direct_user, roles=[await _make_role(db_session, "Direct")])

        members = await _svc.list_members(db_session, project)

        assert [m["user_id"] for m in members] == [direct_user.id]

    async def test_list_members_on_a_group_only_project_is_empty(
        self, db_session: AsyncSession, user: User, project: Project
    ):
        group = await _make_group(db_session, "Developers")
        await _join_group(db_session, group, user)
        await _add_principal(db_session, project, group=group, roles=[await _make_role(db_session, "ViaGroup")])

        assert await _svc.list_members(db_session, project) == []

    async def test_group_membership_does_not_disturb_the_member_row_count(
        self, db_session: AsyncSession, user: User, project: Project
    ):
        group = await _make_group(db_session, "Developers")
        await _add_principal(db_session, project, user=user, roles=[await _make_role(db_session, "Direct")])
        await _add_principal(db_session, project, group=group, roles=[await _make_role(db_session, "ViaGroup")])

        rows = (
            await db_session.execute(select(func.count()).select_from(Member).where(Member.project_id == project.id))
        ).scalar_one()
        assert rows == 2
        assert await _svc.count_membership_rows(db_session, project) == 2


# ---------------------------------------------------------------------------
# The two member counts
# ---------------------------------------------------------------------------


class TestMemberCounts:
    """``count_membership_rows`` counts grants; ``count_people_with_access`` counts humans.

    They agree only while no group is involved. Every assertion here is about
    a case where they must not, so neither number can quietly stand in for
    the other on a screen that means the other one.
    """

    async def test_both_counts_agree_without_groups(self, db_session: AsyncSession, user: User, project: Project):
        await _add_principal(db_session, project, user=user, roles=[await _make_role(db_session, "Direct")])

        assert await _svc.count_membership_rows(db_session, project) == 1
        assert await _svc.count_people_with_access(db_session, project) == 1

    async def test_a_person_in_a_group_and_direct_is_counted_once(
        self, db_session: AsyncSession, user: User, project: Project
    ):
        group = await _make_group(db_session, "Developers")
        await _join_group(db_session, group, user)
        await _add_principal(db_session, project, user=user, roles=[await _make_role(db_session, "Direct")])
        await _add_principal(db_session, project, group=group, roles=[await _make_role(db_session, "ViaGroup")])

        assert await _svc.count_membership_rows(db_session, project) == 2
        assert await _svc.count_people_with_access(db_session, project) == 1

    async def test_a_group_only_project_reports_its_people(
        self, db_session: AsyncSession, user: User, project: Project
    ):
        """The case that made the old card read "0 members" for a staffed project."""
        group = await _make_group(db_session, "Developers")
        await _join_group(db_session, group, user)
        await _add_principal(db_session, project, group=group, roles=[await _make_role(db_session, "ViaGroup")])

        assert await _svc.list_members(db_session, project) == []
        assert await _svc.count_people_with_access(db_session, project) == 1

    async def test_an_empty_group_grants_access_to_nobody(self, db_session: AsyncSession, project: Project):
        group = await _make_group(db_session, "Developers")
        await _add_principal(db_session, project, group=group, roles=[await _make_role(db_session, "ViaGroup")])

        assert await _svc.count_membership_rows(db_session, project) == 1
        assert await _svc.count_people_with_access(db_session, project) == 0


# ---------------------------------------------------------------------------
# Project card / admin table stats
# ---------------------------------------------------------------------------


class TestProjectStats:
    """``load_project_stats`` feeds surfaces that show faces, so it counts people."""

    async def test_stats_count_people_reached_through_a_group(
        self, db_session: AsyncSession, user: User, project: Project
    ):
        group = await _make_group(db_session, "Developers")
        await _join_group(db_session, group, user)
        await _add_principal(db_session, project, group=group, roles=[await _make_role(db_session, "ViaGroup")])

        stats = await _svc.load_project_stats(db_session, [project.id])

        assert stats[project.id]["member_count"] == 1
        assert stats[project.id]["group_count"] == 1
        assert [m["user_id"] for m in stats[project.id]["members"]] == [user.id]

    async def test_stats_do_not_double_count_a_direct_member_who_is_also_in_a_group(
        self, db_session: AsyncSession, user: User, project: Project
    ):
        group = await _make_group(db_session, "Developers")
        await _join_group(db_session, group, user)
        await _add_principal(db_session, project, user=user, roles=[await _make_role(db_session, "Direct")])
        await _add_principal(db_session, project, group=group, roles=[await _make_role(db_session, "ViaGroup")])

        stats = await _svc.load_project_stats(db_session, [project.id])

        assert stats[project.id]["member_count"] == 1
        assert [m["user_id"] for m in stats[project.id]["members"]] == [user.id]

    async def test_group_count_is_zero_without_groups(self, db_session: AsyncSession, user: User, project: Project):
        await _add_principal(db_session, project, user=user, roles=[await _make_role(db_session, "Direct")])

        stats = await _svc.load_project_stats(db_session, [project.id])

        assert stats[project.id]["member_count"] == 1
        assert stats[project.id]["group_count"] == 0
