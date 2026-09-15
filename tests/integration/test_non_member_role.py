"""The seeded Non member role: one row, read-only, never assigned, never removed.

A signed-in user who holds no membership on a public project resolves to this
role. The database keeps it single and system-managed, so no code path has to
be trusted to:

- ``uq_roles_single_builtin`` allows one row per builtin kind;
- ``ck_roles_builtin_not_assignable`` keeps builtin roles unassignable;
- the ``protect_builtin_roles`` trigger refuses deleting a builtin role and
  changing any role's ``builtin``;
- the ``reject_builtin_role_membership`` trigger keeps builtin roles out of
  ``member_roles``.

Its permissions are the one thing an administrator may change, and a change
applies to the next session.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.cli.seed import seed_non_member_role, seed_roles
from specivo.core.exceptions import ValidationError
from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.role import NON_MEMBER_ROLE_NAME, Role, RoleBuiltin
from specivo.models.user import User
from specivo.services.permission_service import Permission, check_permission, get_user_roles
from specivo.services.project_service import Principal, ProjectService
from tests.factories.project import ProjectFactory
from tests.factories.user import UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

_svc = ProjectService()


@pytest_asyncio.fixture
async def non_member_role(db_session: AsyncSession) -> Role:
    return (await db_session.execute(select(Role).where(Role.builtin == RoleBuiltin.NON_MEMBER))).scalar_one()


@pytest_asyncio.fixture
async def public_project(db_session: AsyncSession) -> Project:
    project = ProjectFactory.build(key="NMRPUB", identifier="nmr-public", is_public=True)
    db_session.add(project)
    await db_session.commit()
    return project


@pytest_asyncio.fixture
async def outsider(db_session: AsyncSession) -> User:
    user = UserFactory.build(login="nmr_outsider", status="active")
    db_session.add(user)
    await db_session.commit()
    return user


# ---------------------------------------------------------------------------
# Seeded shape
# ---------------------------------------------------------------------------


async def test_exactly_one_non_member_role_is_seeded(db_session: AsyncSession, non_member_role: Role) -> None:
    count = await db_session.scalar(
        select(func.count()).select_from(Role).where(Role.builtin == RoleBuiltin.NON_MEMBER)
    )
    assert count == 1
    assert non_member_role.name == NON_MEMBER_ROLE_NAME
    assert non_member_role.assignable is False
    assert non_member_role.permissions == [Permission.VIEW_ISSUES]
    assert non_member_role.issues_visibility == "default"


async def test_no_anonymous_role_is_created(db_session: AsyncSession) -> None:
    """Anonymous visitors get a transient role from the project, never a row."""
    count = await db_session.scalar(select(func.count()).select_from(Role).where(Role.builtin == RoleBuiltin.ANONYMOUS))
    assert count == 0


async def test_database_objects_exist(db_session: AsyncSession) -> None:
    index_def = await db_session.scalar(
        text("SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_roles_single_builtin'")
    )
    assert index_def is not None and "UNIQUE" in index_def and "builtin > 0" in index_def

    check = await db_session.scalar(
        text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ck_roles_builtin_not_assignable'")
    )
    assert check is not None

    triggers = set(
        (
            await db_session.execute(
                text(
                    "SELECT tgname FROM pg_trigger WHERE tgname IN "
                    "('trg_protect_builtin_roles', 'trg_reject_builtin_role_membership')"
                )
            )
        ).scalars()
    )
    assert triggers == {"trg_protect_builtin_roles", "trg_reject_builtin_role_membership"}


async def test_a_second_non_member_role_is_rejected(db_session: AsyncSession) -> None:
    db_session.add(Role(name="Second non member", builtin=RoleBuiltin.NON_MEMBER, assignable=False, permissions=[]))
    with pytest.raises(IntegrityError, match="uq_roles_single_builtin"):
        await db_session.flush()


# ---------------------------------------------------------------------------
# Seed CLI
# ---------------------------------------------------------------------------


async def test_seeding_is_idempotent_and_keeps_admin_edits(db_session: AsyncSession, non_member_role: Role) -> None:
    """The entrypoint seeds on every start; it must never reset an administrator's choice."""
    non_member_role.permissions = [Permission.VIEW_ISSUES, Permission.VIEW_WIKI]
    await db_session.commit()

    for _ in range(2):
        await seed_roles(db_session)
        await seed_non_member_role(db_session)

    rows = (await db_session.execute(select(Role).where(Role.builtin == RoleBuiltin.NON_MEMBER))).scalars().all()
    assert len(rows) == 1
    await db_session.refresh(rows[0])
    assert rows[0].id == non_member_role.id
    assert rows[0].permissions == [Permission.VIEW_ISSUES, Permission.VIEW_WIKI]


# ---------------------------------------------------------------------------
# System-managed
# ---------------------------------------------------------------------------


async def test_service_refuses_to_assign_it(
    db_session: AsyncSession, non_member_role: Role, public_project: Project, outsider: User
) -> None:
    with pytest.raises(ValidationError):
        await _svc.add_member(db_session, public_project, Principal.user(outsider.id), [non_member_role.id])


async def test_service_refuses_to_swap_a_membership_onto_it(
    db_session: AsyncSession, non_member_role: Role, public_project: Project, outsider: User
) -> None:
    custom = Role(name="NMR custom", permissions=[Permission.VIEW_ISSUES])
    db_session.add(custom)
    await db_session.flush()
    await _svc.add_member(db_session, public_project, Principal.user(outsider.id), [custom.id])

    with pytest.raises(ValidationError):
        await _svc.update_member_roles(db_session, public_project, Principal.user(outsider.id), [non_member_role.id])


async def test_database_refuses_to_assign_it(
    db_session: AsyncSession, non_member_role: Role, public_project: Project, outsider: User
) -> None:
    member = Member(project_id=public_project.id, user_id=outsider.id)
    db_session.add(member)
    await db_session.flush()

    db_session.add(MemberRole(member_id=member.id, role_id=non_member_role.id))
    with pytest.raises(IntegrityError, match="builtin role cannot be assigned"):
        await db_session.flush()


async def test_it_cannot_be_deleted(db_session: AsyncSession, non_member_role: Role) -> None:
    with pytest.raises(IntegrityError, match="builtin role cannot be deleted"):
        await db_session.execute(text("DELETE FROM roles WHERE id = :id"), {"id": non_member_role.id})


@pytest.mark.parametrize("new_builtin", [RoleBuiltin.CUSTOM, RoleBuiltin.ANONYMOUS])
async def test_its_builtin_cannot_change(db_session: AsyncSession, non_member_role: Role, new_builtin: int) -> None:
    with pytest.raises(IntegrityError, match="builtin cannot change"):
        await db_session.execute(update(Role).where(Role.id == non_member_role.id).values(builtin=new_builtin))


async def test_a_custom_role_cannot_become_builtin(db_session: AsyncSession) -> None:
    custom = Role(name="NMR promote", permissions=[])
    db_session.add(custom)
    await db_session.flush()

    with pytest.raises(IntegrityError, match="builtin cannot change"):
        await db_session.execute(update(Role).where(Role.id == custom.id).values(builtin=RoleBuiltin.ANONYMOUS))


async def test_it_cannot_become_assignable(db_session: AsyncSession, non_member_role: Role) -> None:
    with pytest.raises(IntegrityError, match="ck_roles_builtin_not_assignable"):
        await db_session.execute(update(Role).where(Role.id == non_member_role.id).values(assignable=True))


async def test_admin_edit_to_its_permissions_applies_to_the_next_session(
    db_session: AsyncSession, non_member_role: Role, public_project: Project, outsider: User
) -> None:
    assert await check_permission(outsider, public_project.id, Permission.VIEW_WIKI, db_session) is False

    non_member_role.permissions = [Permission.VIEW_ISSUES, Permission.VIEW_WIKI]
    await db_session.commit()

    # A new request gets a new session, and with it an empty role cache.
    async with AsyncSession(bind=db_session.bind, expire_on_commit=False) as next_session:
        project = await next_session.get(Project, public_project.id)
        user = await next_session.get(User, outsider.id)
        assert await check_permission(user, project.id, Permission.VIEW_WIKI, next_session) is True
        roles = await get_user_roles(next_session, user, project)
        assert [(r.builtin, set(r.permissions)) for r in roles] == [
            (RoleBuiltin.NON_MEMBER, {Permission.VIEW_ISSUES, Permission.VIEW_WIKI})
        ]
