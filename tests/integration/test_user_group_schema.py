"""Schema-level tests for user groups and principal-shaped memberships.

These exercise the database contract rather than a service: case-insensitive
group names, the "exactly one principal" rule on ``members``, and the ON DELETE
CASCADE paths that keep a deleted group or user from leaving orphan rows
behind. Higher layers rely on all of this being enforced by PostgreSQL, so it
is asserted against a real database.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.role import Role
from specivo.models.user import User
from specivo.models.user_group import UserGroup, UserGroupMember
from tests.factories.project import ProjectFactory
from tests.factories.user import UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def user(db_session: AsyncSession) -> User:
    obj = UserFactory.build()
    db_session.add(obj)
    await db_session.flush()
    return obj


@pytest_asyncio.fixture
async def project(db_session: AsyncSession) -> Project:
    obj = ProjectFactory.build()
    db_session.add(obj)
    await db_session.flush()
    return obj


@pytest_asyncio.fixture
async def group(db_session: AsyncSession) -> UserGroup:
    obj = UserGroup(name="Developers", description="Everyone who writes code")
    db_session.add(obj)
    await db_session.flush()
    return obj


async def _count(db: AsyncSession, model, **filters) -> int:
    stmt = select(func.count()).select_from(model)
    for column, value in filters.items():
        stmt = stmt.where(getattr(model, column) == value)
    return (await db.execute(stmt)).scalar_one()


# ---------------------------------------------------------------------------
# Groups and their users
# ---------------------------------------------------------------------------


class TestUserGroup:
    async def test_group_holds_users(self, db_session: AsyncSession, group: UserGroup):
        alice = UserFactory.build()
        bob = UserFactory.build()
        db_session.add_all([alice, bob])
        await db_session.flush()

        db_session.add_all(
            [
                UserGroupMember(group_id=group.id, user_id=alice.id),
                UserGroupMember(group_id=group.id, user_id=bob.id),
            ]
        )
        await db_session.flush()

        assert await _count(db_session, UserGroupMember, group_id=group.id) == 2

    async def test_same_user_cannot_join_twice(self, db_session: AsyncSession, group: UserGroup, user: User):
        db_session.add(UserGroupMember(group_id=group.id, user_id=user.id))
        await db_session.flush()

        db_session.add(UserGroupMember(group_id=group.id, user_id=user.id))
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_name_is_unique_case_insensitively(self, db_session: AsyncSession, group: UserGroup):
        """A lowercase spelling must not coexist with the capitalised one."""
        db_session.add(UserGroup(name="developers"))
        with pytest.raises(IntegrityError):
            await db_session.flush()


# ---------------------------------------------------------------------------
# members.user_id / members.group_id — exactly one principal
# ---------------------------------------------------------------------------


class TestMemberPrincipal:
    async def test_group_membership_needs_no_user(self, db_session: AsyncSession, group: UserGroup, project: Project):
        member = Member(group_id=group.id, project_id=project.id)
        db_session.add(member)
        await db_session.flush()

        assert member.id is not None
        assert member.user_id is None

    async def test_user_membership_still_works(self, db_session: AsyncSession, user: User, project: Project):
        member = Member(user_id=user.id, project_id=project.id)
        db_session.add(member)
        await db_session.flush()

        assert member.id is not None
        assert member.group_id is None

    async def test_both_principals_rejected(
        self, db_session: AsyncSession, user: User, group: UserGroup, project: Project
    ):
        db_session.add(Member(user_id=user.id, group_id=group.id, project_id=project.id))
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_no_principal_rejected(self, db_session: AsyncSession, project: Project):
        db_session.add(Member(project_id=project.id))
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_group_joins_a_project_once(self, db_session: AsyncSession, group: UserGroup, project: Project):
        db_session.add(Member(group_id=group.id, project_id=project.id))
        await db_session.flush()

        db_session.add(Member(group_id=group.id, project_id=project.id))
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_user_and_group_memberships_coexist_on_one_project(
        self, db_session: AsyncSession, user: User, group: UserGroup, project: Project
    ):
        """The two unique constraints must not collide over each other's NULLs."""
        db_session.add_all(
            [
                Member(user_id=user.id, project_id=project.id),
                Member(group_id=group.id, project_id=project.id),
            ]
        )
        await db_session.flush()

        assert await _count(db_session, Member, project_id=project.id) == 2


# ---------------------------------------------------------------------------
# Roles on a group-held membership
# ---------------------------------------------------------------------------


class TestGroupMemberRoles:
    async def test_roles_attach_to_a_group_membership(
        self, db_session: AsyncSession, group: UserGroup, project: Project
    ):
        role = Role(name=f"Dev-{group.id}", permissions=["view_issues"], builtin=0)
        member = Member(group_id=group.id, project_id=project.id)
        db_session.add_all([role, member])
        await db_session.flush()

        db_session.add(MemberRole(member_id=member.id, role_id=role.id))
        await db_session.flush()

        assert await _count(db_session, MemberRole, member_id=member.id) == 1


# ---------------------------------------------------------------------------
# Cascades
# ---------------------------------------------------------------------------


class TestCascades:
    async def test_deleting_a_group_removes_its_rows(
        self, db_session: AsyncSession, group: UserGroup, user: User, project: Project
    ):
        db_session.add_all(
            [
                UserGroupMember(group_id=group.id, user_id=user.id),
                Member(group_id=group.id, project_id=project.id),
            ]
        )
        await db_session.flush()
        group_id = group.id

        await db_session.execute(text("DELETE FROM user_groups WHERE id = :id"), {"id": group_id})

        assert await _count(db_session, UserGroupMember, group_id=group_id) == 0
        assert await _count(db_session, Member, group_id=group_id) == 0

    async def test_deleting_a_group_leaves_user_memberships_alone(
        self, db_session: AsyncSession, group: UserGroup, user: User, project: Project
    ):
        db_session.add(Member(user_id=user.id, project_id=project.id))
        db_session.add(Member(group_id=group.id, project_id=project.id))
        await db_session.flush()
        project_id, user_id = project.id, user.id

        await db_session.execute(text("DELETE FROM user_groups WHERE id = :id"), {"id": group.id})

        assert await _count(db_session, Member, project_id=project_id, user_id=user_id) == 1

    async def test_deleting_a_user_removes_its_group_rows(self, db_session: AsyncSession, group: UserGroup, user: User):
        db_session.add(UserGroupMember(group_id=group.id, user_id=user.id))
        await db_session.flush()
        user_id, group_id = user.id, group.id

        await db_session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})

        assert await _count(db_session, UserGroupMember, user_id=user_id) == 0
        assert await _count(db_session, UserGroup, id=group_id) == 1
