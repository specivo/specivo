"""Shared versions are visible only to readers who can open the owning project.

``VersionService.visible_versions`` decides which versions of related projects
reach a project through sharing. Sharing widens the set of projects a version
reaches; it must never show a reader a version whose owning project they
cannot open. The filter is ``ProjectService.accessible_projects_clause``, the
predicate the project list uses, so the rules here are the project rules:
admins see everything, signed-in users see public projects and the ones they
are members of, and the anonymous user sees only public, active, opted-in
projects while the instance switch is on.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.role import Role
from specivo.models.user import User
from specivo.models.version import Version
from specivo.schemas.version import VersionCreate
from specivo.services.anonymous_access_service import (
    set_anonymous_access_enabled,
    set_anonymous_permissions,
)
from specivo.services.anonymous_user_service import get_anonymous_user
from specivo.services.permission_service import Permission
from specivo.services.version_service import VersionService
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.service]

_svc = VersionService()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _user(db: AsyncSession, login: str, *, admin: bool = False) -> User:
    factory = AdminUserFactory if admin else UserFactory
    user = factory.build(login=login, status="active")
    db.add(user)
    await db.commit()
    return user


async def _project(
    db: AsyncSession,
    key: str,
    *,
    public: bool,
    parent: Project | None = None,
) -> Project:
    identifier = key.lower()
    path = f"{parent.path}.{identifier}" if parent else identifier
    project = ProjectFactory.build(
        key=key,
        identifier=identifier,
        path=path,
        is_public=public,
        parent_id=parent.id if parent else None,
    )
    db.add(project)
    await db.commit()
    return project


async def _add_member(db: AsyncSession, user: User, project: Project) -> None:
    role = Role(name=f"Role-{project.key}", permissions=["view_issues"], builtin=0, issues_visibility="default")
    db.add(role)
    await db.flush()
    member = Member(user_id=user.id, project_id=project.id)
    db.add(member)
    await db.flush()
    db.add(MemberRole(member_id=member.id, role_id=role.id))
    await db.commit()


async def _version(db: AsyncSession, project: Project, name: str, sharing: str) -> Version:
    version = await _svc.create(db, project, VersionCreate(name=name, sharing=sharing))
    await db.commit()
    return version


async def _visible_ids(db: AsyncSession, project: Project, reader: User) -> set[int]:
    return {v.id for v in await _svc.visible_versions(db, project, reader)}


# ---------------------------------------------------------------------------
# A private child's hierarchy-shared version, seen from its public parent
# ---------------------------------------------------------------------------


async def _private_child_with_shared_version(db: AsyncSession) -> tuple[Project, Project, Version]:
    parent = await _project(db, "VSVPAR", public=True)
    child = await _project(db, "VSVCHI", public=False, parent=parent)
    version = await _version(db, child, "child-release", "hierarchy")
    return parent, child, version


async def test_private_child_shared_version_hidden_from_non_member(db_session: AsyncSession) -> None:
    parent, _child, version = await _private_child_with_shared_version(db_session)
    outsider = await _user(db_session, "vsv_outsider")

    assert version.id not in await _visible_ids(db_session, parent, outsider)


async def test_private_child_shared_version_shown_to_member(db_session: AsyncSession) -> None:
    parent, child, version = await _private_child_with_shared_version(db_session)
    member = await _user(db_session, "vsv_member")
    await _add_member(db_session, member, child)

    assert version.id in await _visible_ids(db_session, parent, member)


async def test_private_child_shared_version_shown_to_admin(db_session: AsyncSession) -> None:
    parent, _child, version = await _private_child_with_shared_version(db_session)
    admin = await _user(db_session, "vsv_admin", admin=True)

    assert version.id in await _visible_ids(db_session, parent, admin)


# ---------------------------------------------------------------------------
# System sharing follows the same rule
# ---------------------------------------------------------------------------


async def test_private_project_system_version_needs_membership(db_session: AsyncSession) -> None:
    owner = await _project(db_session, "VSVSYS", public=False)
    other = await _project(db_session, "VSVOTH", public=True)
    version = await _version(db_session, owner, "everywhere", "system")
    outsider = await _user(db_session, "vsv_sys_outsider")
    member = await _user(db_session, "vsv_sys_member")
    await _add_member(db_session, member, owner)

    assert version.id not in await _visible_ids(db_session, other, outsider)
    assert version.id in await _visible_ids(db_session, other, member)


async def test_public_project_shared_version_visible_to_non_member(db_session: AsyncSession) -> None:
    parent = await _project(db_session, "VSVPUB", public=True)
    child = await _project(db_session, "VSVPUC", public=True, parent=parent)
    tree_version = await _version(db_session, child, "tree-release", "tree")
    system_version = await _version(db_session, child, "system-release", "system")
    outsider = await _user(db_session, "vsv_pub_outsider")

    visible = await _visible_ids(db_session, parent, outsider)
    assert {tree_version.id, system_version.id} <= visible


# ---------------------------------------------------------------------------
# sharing='none' never leaves its project
# ---------------------------------------------------------------------------


async def test_none_sharing_is_never_cross_project(db_session: AsyncSession) -> None:
    parent = await _project(db_session, "VSVNOP", public=True)
    child = await _project(db_session, "VSVNOC", public=True, parent=parent)
    unrelated = await _project(db_session, "VSVNOU", public=True)
    version = await _version(db_session, child, "local-only", "none")
    admin = await _user(db_session, "vsv_none_admin", admin=True)
    member = await _user(db_session, "vsv_none_member")
    await _add_member(db_session, member, child)

    for reader in (admin, member):
        assert version.id not in await _visible_ids(db_session, parent, reader)
        assert version.id not in await _visible_ids(db_session, unrelated, reader)
        assert version.id in await _visible_ids(db_session, child, reader)


# ---------------------------------------------------------------------------
# The anonymous reader
# ---------------------------------------------------------------------------


async def test_anonymous_sees_only_versions_of_readable_projects(db_session: AsyncSession) -> None:
    admin = await _user(db_session, "vsv_anon_admin", admin=True)
    opted_in = await _project(db_session, "VSVAOK", public=True)
    not_opted_in = await _project(db_session, "VSVANO", public=True)
    archived = await _project(db_session, "VSVAAR", public=True)
    private = await _project(db_session, "VSVAPR", public=False)

    await set_anonymous_permissions(db_session, opted_in, [Permission.VIEW_ISSUES], admin)
    await set_anonymous_permissions(db_session, archived, [Permission.VIEW_ISSUES], admin)
    await set_anonymous_access_enabled(db_session, True, admin, confirmed_projects=[opted_in.key, archived.key])
    archived.status = 9
    await db_session.commit()

    readable = await _version(db_session, opted_in, "anon-readable", "system")
    hidden = [
        await _version(db_session, not_opted_in, "anon-not-opted-in", "system"),
        await _version(db_session, archived, "anon-archived", "system"),
        await _version(db_session, private, "anon-private", "system"),
    ]

    anonymous = await get_anonymous_user(db_session)
    assert anonymous is not None
    visible = await _visible_ids(db_session, opted_in, anonymous)
    assert readable.id in visible
    assert not visible & {v.id for v in hidden}


async def test_anonymous_sees_nothing_while_switch_is_off(db_session: AsyncSession) -> None:
    project = await _project(db_session, "VSVOFF", public=True)
    version = await _version(db_session, project, "switch-off", "system")

    anonymous = await get_anonymous_user(db_session)
    assert anonymous is not None
    assert version.id not in await _visible_ids(db_session, project, anonymous)
