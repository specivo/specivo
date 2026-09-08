"""Service-layer tests for ``UserGroupService``.

A user group is a membership principal, so the interesting behaviour here is
not CRUD but what the writes do to access: a case-insensitively unique name,
an idempotent "add user", and a delete that revokes project access everywhere
at once and reports how much it revoked.

Group and role names are unique database-wide and test modules run in parallel
inside uncommitted transactions, so every name built here carries a
module-local prefix and a counter — two modules inserting the same name would
block on the unique index.
"""

from __future__ import annotations

import itertools

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import ConflictError, NotFoundError
from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.role import Role
from specivo.models.user import User
from specivo.models.user_group import UserGroup, UserGroupMember
from specivo.services.user_group_service import UserGroupService
from tests.factories.project import ProjectFactory
from tests.factories.user import UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.service]

_svc = UserGroupService()

# Module-local name prefix — see the module docstring.
_PREFIX = "ugsvc"
_counter = itertools.count(1)


def _name(stem: str) -> str:
    return f"{_PREFIX}-{stem}-{next(_counter)}"


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
async def group(db_session: AsyncSession) -> UserGroup:
    return await _svc.create(db_session, name=_name("Developers"), description="Writes code")


async def _make_user(db: AsyncSession, **kwargs) -> User:
    obj = UserFactory.build(**kwargs)
    db.add(obj)
    await db.flush()
    return obj


async def _make_project(db: AsyncSession) -> Project:
    obj = ProjectFactory.build()
    db.add(obj)
    await db.flush()
    return obj


async def _grant(db: AsyncSession, group: UserGroup, project: Project, role_names: list[str]) -> Member:
    """Give *group* a membership on *project* holding roles named *role_names*."""
    member = Member(group_id=group.id, project_id=project.id)
    db.add(member)
    await db.flush()
    for role_name in role_names:
        role = Role(name=role_name, permissions=["view_issues"], builtin=0, issues_visibility="default")
        db.add(role)
        await db.flush()
        db.add(MemberRole(member_id=member.id, role_id=role.id))
    await db.flush()
    return member


async def _count(db: AsyncSession, model, **filters) -> int:
    stmt = select(func.count()).select_from(model)
    for column, value in filters.items():
        stmt = stmt.where(getattr(model, column) == value)
    return (await db.execute(stmt)).scalar_one()


# ---------------------------------------------------------------------------
# Create / get
# ---------------------------------------------------------------------------


class TestCreate:
    async def test_create_returns_the_group(self, db_session: AsyncSession):
        name = _name("Backend")
        group = await _svc.create(db_session, name=name, description="Server side")

        assert group.id is not None
        assert group.name == name
        assert group.description == "Server side"

    async def test_description_is_optional(self, db_session: AsyncSession):
        group = await _svc.create(db_session, name=_name("Minimal"))
        assert group.description is None

    async def test_duplicate_name_raises_conflict(self, db_session: AsyncSession):
        name = _name("Duplicate")
        await _svc.create(db_session, name=name)

        with pytest.raises(ConflictError):
            await _svc.create(db_session, name=name)

    async def test_duplicate_name_is_detected_ignoring_case(self, db_session: AsyncSession):
        """The DB index is case-insensitive; the service must raise before it fires."""
        name = _name("CaseFold")
        await _svc.create(db_session, name=name)

        with pytest.raises(ConflictError):
            await _svc.create(db_session, name=name.upper())


class TestGet:
    async def test_get_returns_the_group(self, db_session: AsyncSession, group: UserGroup):
        assert (await _svc.get(db_session, group.id)).id == group.id

    async def test_missing_group_raises_not_found(self, db_session: AsyncSession):
        with pytest.raises(NotFoundError):
            await _svc.get(db_session, 987654321)


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


class TestUpdate:
    async def test_rename(self, db_session: AsyncSession, group: UserGroup):
        new_name = _name("Renamed")
        updated = await _svc.update(db_session, group.id, name=new_name)
        assert updated.name == new_name

    async def test_rename_onto_an_existing_name_raises_conflict(self, db_session: AsyncSession, group: UserGroup):
        taken = _name("Taken")
        await _svc.create(db_session, name=taken)

        with pytest.raises(ConflictError):
            await _svc.update(db_session, group.id, name=taken.upper())

    async def test_a_group_can_be_recased(self, db_session: AsyncSession, group: UserGroup):
        """Renaming a group to a different casing of its own name is not a collision."""
        updated = await _svc.update(db_session, group.id, name=group.name.upper())
        assert updated.name == updated.name.upper()

    async def test_description_can_be_changed(self, db_session: AsyncSession, group: UserGroup):
        updated = await _svc.update(db_session, group.id, description="Now something else")
        assert updated.description == "Now something else"

    async def test_description_can_be_cleared(self, db_session: AsyncSession, group: UserGroup):
        updated = await _svc.update(db_session, group.id, description=None)
        assert updated.description is None

    async def test_omitting_description_leaves_it_alone(self, db_session: AsyncSession, group: UserGroup):
        original = group.description
        updated = await _svc.update(db_session, group.id, name=_name("Still"))
        assert updated.description == original

    async def test_missing_group_raises_not_found(self, db_session: AsyncSession):
        with pytest.raises(NotFoundError):
            await _svc.update(db_session, 987654321, name=_name("Ghost"))


# ---------------------------------------------------------------------------
# Group membership
# ---------------------------------------------------------------------------


class TestUsers:
    async def test_add_user(self, db_session: AsyncSession, group: UserGroup, user: User):
        assert await _svc.add_user(db_session, group.id, user.id) is True
        assert await _svc.count_users(db_session, group.id) == 1

    async def test_adding_the_same_user_twice_is_a_no_op(self, db_session: AsyncSession, group: UserGroup, user: User):
        await _svc.add_user(db_session, group.id, user.id)

        assert await _svc.add_user(db_session, group.id, user.id) is False
        assert await _svc.count_users(db_session, group.id) == 1

    async def test_adding_to_a_missing_group_raises_not_found(self, db_session: AsyncSession, user: User):
        with pytest.raises(NotFoundError):
            await _svc.add_user(db_session, 987654321, user.id)

    async def test_adding_a_missing_user_raises_not_found(self, db_session: AsyncSession, group: UserGroup):
        with pytest.raises(NotFoundError):
            await _svc.add_user(db_session, group.id, 987654321)

    async def test_remove_user(self, db_session: AsyncSession, group: UserGroup, user: User):
        await _svc.add_user(db_session, group.id, user.id)

        await _svc.remove_user(db_session, group.id, user.id)

        assert await _svc.count_users(db_session, group.id) == 0

    async def test_removing_a_non_member_raises_not_found(self, db_session: AsyncSession, group: UserGroup, user: User):
        with pytest.raises(NotFoundError):
            await _svc.remove_user(db_session, group.id, user.id)

    async def test_list_users(self, db_session: AsyncSession, group: UserGroup):
        alice = await _make_user(db_session)
        bob = await _make_user(db_session)
        await _svc.add_user(db_session, group.id, alice.id)
        await _svc.add_user(db_session, group.id, bob.id)

        rows, total = await _svc.list_users(db_session, group.id)

        assert total == 2
        assert {r["user_id"] for r in rows} == {alice.id, bob.id}
        assert all("login" in r and "display_name" in r for r in rows)

    async def test_list_users_paginates(self, db_session: AsyncSession, group: UserGroup):
        for _ in range(3):
            await _svc.add_user(db_session, group.id, (await _make_user(db_session)).id)

        rows, total = await _svc.list_users(db_session, group.id, offset=1, limit=1)

        assert total == 3
        assert len(rows) == 1

    async def test_list_users_of_a_missing_group_raises_not_found(self, db_session: AsyncSession):
        with pytest.raises(NotFoundError):
            await _svc.list_users(db_session, 987654321)

    async def test_list_user_groups(self, db_session: AsyncSession, user: User):
        first = await _svc.create(db_session, name=_name("AAA"))
        second = await _svc.create(db_session, name=_name("BBB"))
        await _svc.add_user(db_session, first.id, user.id)
        await _svc.add_user(db_session, second.id, user.id)

        groups = await _svc.list_user_groups(db_session, user.id)

        assert [g.id for g in groups] == [first.id, second.id]

    async def test_list_user_groups_is_empty_for_a_loner(self, db_session: AsyncSession, user: User):
        assert await _svc.list_user_groups(db_session, user.id) == []


# ---------------------------------------------------------------------------
# Listing groups
# ---------------------------------------------------------------------------


class TestListGroups:
    async def test_rows_carry_the_counts(self, db_session: AsyncSession, group: UserGroup, user: User):
        await _svc.add_user(db_session, group.id, user.id)
        await _grant(db_session, group, await _make_project(db_session), [_name("Role")])

        rows, _ = await _svc.list_groups(db_session, q=group.name)

        assert len(rows) == 1
        assert rows[0]["user_count"] == 1
        assert rows[0]["project_count"] == 1

    async def test_counts_are_zero_for_an_empty_group(self, db_session: AsyncSession, group: UserGroup):
        rows, _ = await _svc.list_groups(db_session, q=group.name)
        assert rows[0]["user_count"] == 0
        assert rows[0]["project_count"] == 0

    async def test_search_is_case_insensitive(self, db_session: AsyncSession, group: UserGroup):
        rows, total = await _svc.list_groups(db_session, q=group.name.upper())
        assert total == 1
        assert rows[0]["id"] == group.id

    async def test_search_that_matches_nothing_returns_nothing(self, db_session: AsyncSession, group: UserGroup):
        rows, total = await _svc.list_groups(db_session, q=f"{_PREFIX}-no-such-group")
        assert rows == []
        assert total == 0

    async def test_pagination_reports_the_full_total(self, db_session: AsyncSession):
        stem = f"{_PREFIX}-paged"
        for index in range(3):
            await _svc.create(db_session, name=f"{stem}-{index}")

        rows, total = await _svc.list_groups(db_session, q=stem, offset=1, limit=1)

        assert total == 3
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# Projects the group grants access to
# ---------------------------------------------------------------------------


class TestListProjects:
    async def test_lists_projects_with_their_roles(self, db_session: AsyncSession, group: UserGroup):
        project = await _make_project(db_session)
        reviewer, developer = _name("Reviewer"), _name("Developer")
        await _grant(db_session, group, project, [developer, reviewer])

        projects = await _svc.list_projects(db_session, group.id)

        assert len(projects) == 1
        assert projects[0]["project_id"] == project.id
        assert projects[0]["key"] == project.key
        assert sorted(projects[0]["roles"]) == sorted([developer, reviewer])

    async def test_a_membership_without_roles_still_appears(self, db_session: AsyncSession, group: UserGroup):
        """An outer join, so a role-less membership is not silently dropped."""
        project = await _make_project(db_session)
        await _grant(db_session, group, project, [])

        projects = await _svc.list_projects(db_session, group.id)

        assert [p["project_id"] for p in projects] == [project.id]
        assert projects[0]["roles"] == []

    async def test_a_group_with_no_memberships_lists_nothing(self, db_session: AsyncSession, group: UserGroup):
        assert await _svc.list_projects(db_session, group.id) == []


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


class TestDelete:
    async def test_delete_removes_the_group(self, db_session: AsyncSession, group: UserGroup):
        group_id = group.id
        await _svc.delete(db_session, group_id)

        with pytest.raises(NotFoundError):
            await _svc.get(db_session, group_id)

    async def test_delete_reports_what_it_removed(self, db_session: AsyncSession, group: UserGroup):
        await _svc.add_user(db_session, group.id, (await _make_user(db_session)).id)
        await _svc.add_user(db_session, group.id, (await _make_user(db_session)).id)
        await _grant(db_session, group, await _make_project(db_session), [_name("Role")])
        await _grant(db_session, group, await _make_project(db_session), [_name("Role")])

        removed = await _svc.delete(db_session, group.id)

        assert removed.group_id == group.id
        assert removed.name == group.name
        assert removed.users_removed == 2
        assert removed.project_memberships_removed == 2

    async def test_delete_cascades_the_project_memberships_away(self, db_session: AsyncSession, group: UserGroup):
        project = await _make_project(db_session)
        await _svc.add_user(db_session, group.id, (await _make_user(db_session)).id)
        await _grant(db_session, group, project, [_name("Role")])
        group_id = group.id

        await _svc.delete(db_session, group_id)

        assert await _count(db_session, Member, group_id=group_id) == 0
        assert await _count(db_session, UserGroupMember, group_id=group_id) == 0

    async def test_delete_leaves_user_held_memberships_alone(
        self, db_session: AsyncSession, group: UserGroup, user: User
    ):
        project = await _make_project(db_session)
        db_session.add(Member(user_id=user.id, project_id=project.id))
        await db_session.flush()
        await _grant(db_session, group, project, [_name("Role")])

        await _svc.delete(db_session, group.id)

        assert await _count(db_session, Member, project_id=project.id, user_id=user.id) == 1

    async def test_deleting_a_missing_group_raises_not_found(self, db_session: AsyncSession):
        with pytest.raises(NotFoundError):
            await _svc.delete(db_session, 987654321)
