"""Role resolution for the anonymous principal, and how its inputs are read.

The anonymous user holds no row in ``roles`` or ``members``. What it may read
in a project is a transient role built from the project's
``anonymous_permissions``, and only while the instance switch is on and the
project is active. These tests pin the properties that make that safe:

- a hard ceiling inside ``check_permission``: nothing outside
  ``view_issues`` / ``view_wiki`` is ever granted, whatever the project's JSON
  says;
- nothing about anonymous access is cached, so turning the switch off or
  emptying a project's list applies to the very next call;
- the membership role cache lives in ``session.info``: it is per session and
  never seen by another one;
- ``require_project_access`` refuses anonymous visitors with one exception,
  identical for every reason;
- a wiki GET by someone who may not edit the wiki writes nothing.
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.core.exceptions import AnonymousAccessDeniedError
from specivo.models.member import Member, MemberRole
from specivo.models.project import EnabledModule, Project
from specivo.models.role import Role, RoleBuiltin
from specivo.models.user import User
from specivo.models.wiki import Wiki, WikiContent, WikiPage
from specivo.services.anonymous_access_service import set_anonymous_access_enabled, set_anonymous_permissions
from specivo.services.anonymous_user_service import get_anonymous_user
from specivo.services.auth_service import _make_access_token
from specivo.services.permission_service import (
    ANONYMOUS_PERMISSION_CEILING,
    Permission,
    check_permission,
    clear_role_cache,
    get_user_roles,
)
from specivo.services.project_service import ProjectService
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

_svc = ProjectService()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def admin(db_session: AsyncSession) -> User:
    user = AdminUserFactory.build(login="arr_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    return user


@pytest_asyncio.fixture
async def anonymous(db_session: AsyncSession) -> User:
    user = await get_anonymous_user(db_session)
    assert user is not None
    return user


@pytest_asyncio.fixture
async def outsider(db_session: AsyncSession) -> User:
    user = UserFactory.build(login="arr_outsider", status="active")
    db_session.add(user)
    await db_session.commit()
    return user


async def _project(db: AsyncSession, key: str, *, is_public: bool = True, status: int = 1) -> Project:
    project = ProjectFactory.build(key=key, identifier=key.lower(), is_public=is_public, status=status)
    db.add(project)
    await db.flush()
    for module in ("issue_tracking", "wiki"):
        db.add(EnabledModule(project_id=project.id, name=module))
    await db.commit()
    return project


@pytest_asyncio.fixture
async def opted_in(db_session: AsyncSession, admin: User) -> Project:
    """A public, active project opted in to both permissions, with the switch on."""
    project = await _project(db_session, "ARROPT")
    await set_anonymous_permissions(db_session, project, [Permission.VIEW_ISSUES, Permission.VIEW_WIKI], admin)
    await set_anonymous_access_enabled(db_session, True, admin, confirmed_projects=[project.key])
    await db_session.commit()
    return project


# ---------------------------------------------------------------------------
# Ceiling
# ---------------------------------------------------------------------------


async def test_ceiling_is_exactly_the_two_read_permissions() -> None:
    assert ANONYMOUS_PERMISSION_CEILING == {Permission.VIEW_ISSUES, Permission.VIEW_WIKI}


@pytest.mark.parametrize(
    "permission",
    [p for p in Permission if p not in ANONYMOUS_PERMISSION_CEILING] + ["*", "anything_else"],
)
async def test_anonymous_is_denied_everything_outside_the_ceiling(
    db_session: AsyncSession, anonymous: User, opted_in: Project, permission: str
) -> None:
    assert await check_permission(anonymous, opted_in.id, permission, db_session) is False


@pytest.mark.parametrize("permission", sorted(ANONYMOUS_PERMISSION_CEILING))
async def test_anonymous_is_granted_what_the_project_opted_in_to(
    db_session: AsyncSession, anonymous: User, opted_in: Project, permission: str
) -> None:
    assert await check_permission(anonymous, opted_in.id, permission, db_session) is True


@pytest.mark.parametrize(
    ("widened", "permission", "expected"),
    [
        (["*"], Permission.VIEW_ISSUES, False),
        (["*"], "*", False),
        (["view_issues", "add_issues", "*"], Permission.ADD_ISSUES, False),
        (["view_issues", "add_issues", "*"], Permission.MANAGE_PROJECT, False),
        (["view_issues", "add_issues", "*"], Permission.VIEW_ISSUES, True),
        (["view_wiki", "manage_wiki"], Permission.MANAGE_WIKI, False),
    ],
)
async def test_ceiling_holds_even_if_the_projects_json_were_widened(
    db_session: AsyncSession,
    anonymous: User,
    opted_in: Project,
    widened: list[str],
    permission: str,
    expected: bool,
) -> None:
    """The CHECK constraint forbids this, so the widening is done in memory only."""
    with db_session.no_autoflush:
        opted_in.anonymous_permissions = widened
        assert await check_permission(anonymous, opted_in.id, permission, db_session) is expected
        roles = await get_user_roles(db_session, anonymous, opted_in)
        assert all(set(role.permissions) <= ANONYMOUS_PERMISSION_CEILING for role in roles)
    db_session.expire(opted_in)


async def test_ceiling_applies_to_the_non_member_fallback_too(
    db_session: AsyncSession, outsider: User, opted_in: Project
) -> None:
    """The transient role a signed-in non-member also receives is capped the same way."""
    with db_session.no_autoflush:
        opted_in.anonymous_permissions = ["view_wiki", "manage_wiki", "*"]
        roles = await get_user_roles(db_session, outsider, opted_in)
        transient = [r for r in roles if r.transient]
        assert [set(r.permissions) for r in transient] == [{Permission.VIEW_WIKI}]
        assert await check_permission(outsider, opted_in.id, Permission.MANAGE_WIKI, db_session) is False
    db_session.expire(opted_in)


# ---------------------------------------------------------------------------
# Timing: nothing anonymous is cached
# ---------------------------------------------------------------------------


async def test_turning_the_switch_off_applies_to_the_next_call(
    db_session: AsyncSession, admin: User, anonymous: User, outsider: User, opted_in: Project
) -> None:
    assert await check_permission(anonymous, opted_in.id, Permission.VIEW_ISSUES, db_session) is True
    assert await check_permission(outsider, opted_in.id, Permission.VIEW_WIKI, db_session) is True

    await set_anonymous_access_enabled(db_session, False, admin)

    # Same session, no cache clearing.
    assert await check_permission(anonymous, opted_in.id, Permission.VIEW_ISSUES, db_session) is False
    assert await get_user_roles(db_session, anonymous, opted_in) == []
    assert await check_permission(outsider, opted_in.id, Permission.VIEW_WIKI, db_session) is False
    with pytest.raises(AnonymousAccessDeniedError):
        await _svc.require_project_access(db_session, opted_in, anonymous)


async def test_clearing_the_projects_list_applies_to_the_next_call(
    db_session: AsyncSession, admin: User, anonymous: User, outsider: User, opted_in: Project
) -> None:
    assert await check_permission(anonymous, opted_in.id, Permission.VIEW_WIKI, db_session) is True

    await set_anonymous_permissions(db_session, opted_in, [Permission.VIEW_ISSUES], admin)

    assert await check_permission(anonymous, opted_in.id, Permission.VIEW_WIKI, db_session) is False
    assert await check_permission(anonymous, opted_in.id, Permission.VIEW_ISSUES, db_session) is True
    assert await check_permission(outsider, opted_in.id, Permission.VIEW_WIKI, db_session) is False

    await set_anonymous_permissions(db_session, opted_in, [], admin)

    assert await get_user_roles(db_session, anonymous, opted_in) == []
    with pytest.raises(AnonymousAccessDeniedError):
        await _svc.require_project_access(db_session, opted_in, anonymous)


async def test_archiving_the_project_applies_to_the_next_call(
    db_session: AsyncSession, anonymous: User, opted_in: Project
) -> None:
    assert await check_permission(anonymous, opted_in.id, Permission.VIEW_ISSUES, db_session) is True

    opted_in.status = 9
    await db_session.flush()

    assert await check_permission(anonymous, opted_in.id, Permission.VIEW_ISSUES, db_session) is False


async def test_anonymous_resolution_leaves_nothing_in_the_session_cache(
    db_session: AsyncSession, anonymous: User, opted_in: Project
) -> None:
    clear_role_cache(db_session)
    await get_user_roles(db_session, anonymous, opted_in)
    await check_permission(anonymous, opted_in.id, Permission.VIEW_ISSUES, db_session)

    cache: dict[Any, Any] = db_session.info.get("specivo.role_cache", {})
    # Membership entries are keyed ("member", user_id, project_id).
    assert not [key for key in cache if isinstance(key, tuple) and key[1] == anonymous.id]


# ---------------------------------------------------------------------------
# The membership cache is per session
# ---------------------------------------------------------------------------


async def test_membership_cache_is_per_session(db_session: AsyncSession, admin: User, outsider: User) -> None:
    project = await _project(db_session, "ARRCACHE", is_public=False)
    role = Role(name="ARR cache role", permissions=[Permission.ADD_ISSUES])
    db_session.add(role)
    await db_session.flush()
    member = Member(project_id=project.id, user_id=outsider.id)
    db_session.add(member)
    await db_session.flush()
    db_session.add(MemberRole(member_id=member.id, role_id=role.id))
    await db_session.commit()

    assert await check_permission(outsider, project.id, Permission.ADD_ISSUES, db_session) is True

    await db_session.delete(member)
    await db_session.commit()

    # The session that resolved the roles keeps its answer for its own lifetime ...
    assert await check_permission(outsider, project.id, Permission.ADD_ISSUES, db_session) is True

    # ... another session never sees it ...
    async with AsyncSession(bind=db_session.bind, expire_on_commit=False) as other:
        other_user = await other.get(User, outsider.id)
        other_project = await other.get(Project, project.id)
        assert "specivo.role_cache" not in other.info
        assert await check_permission(other_user, other_project.id, Permission.ADD_ISSUES, other) is False

    # ... and clearing it re-reads.
    clear_role_cache(db_session)
    assert await check_permission(outsider, project.id, Permission.ADD_ISSUES, db_session) is False


# ---------------------------------------------------------------------------
# require_project_access: one uniform denial
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", ["private", "public_not_opted_in", "archived_opted_in", "switch_off"])
async def test_anonymous_denial_is_uniform(
    db_session: AsyncSession, admin: User, anonymous: User, opted_in: Project, case: str
) -> None:
    if case == "private":
        project = await _project(db_session, "ARRPRIV", is_public=False)
    elif case == "public_not_opted_in":
        project = await _project(db_session, "ARRPUB")
    elif case == "archived_opted_in":
        project = await _project(db_session, "ARRARC", status=9)
        await set_anonymous_permissions(db_session, project, [Permission.VIEW_ISSUES], admin)
    else:
        project = opted_in
        await set_anonymous_access_enabled(db_session, False, admin)

    with pytest.raises(AnonymousAccessDeniedError) as denied:
        await _svc.require_project_access(db_session, project, anonymous)

    reference = AnonymousAccessDeniedError()
    assert (denied.value.code, denied.value.message, denied.value.status_code) == (
        reference.code,
        reference.message,
        reference.status_code,
    )
    assert project.key not in denied.value.message


async def test_anonymous_reaches_an_opted_in_project(
    db_session: AsyncSession, anonymous: User, opted_in: Project
) -> None:
    await _svc.require_project_access(db_session, opted_in, anonymous)


async def test_wiki_only_opt_in_is_enough_to_reach_the_project(
    db_session: AsyncSession, admin: User, anonymous: User, opted_in: Project
) -> None:
    await set_anonymous_permissions(db_session, opted_in, [Permission.VIEW_WIKI], admin)
    await _svc.require_project_access(db_session, opted_in, anonymous)
    assert await check_permission(anonymous, opted_in.id, Permission.VIEW_ISSUES, db_session) is False


# ---------------------------------------------------------------------------
# Wiki GET writes nothing without edit rights
# ---------------------------------------------------------------------------


async def _wiki_row_counts(db: AsyncSession) -> tuple[int, int, int]:
    return (
        await db.scalar(select(func.count()).select_from(Wiki)) or 0,
        await db.scalar(select(func.count()).select_from(WikiPage)) or 0,
        await db.scalar(select(func.count()).select_from(WikiContent)) or 0,
    )


async def _get(client: AsyncClient, user: User, path: str) -> int:
    token = _make_access_token(user, get_settings())
    resp = await client.get(path, cookies={"access_token": token}, follow_redirects=False)
    return resp.status_code


@pytest.mark.parametrize("reader", ["non_member", "member_with_view_wiki", "non_member_with_anonymous_wiki"])
async def test_wiki_get_by_a_reader_without_edit_rights_writes_nothing(
    client: AsyncClient, db_session: AsyncSession, admin: User, outsider: User, reader: str
) -> None:
    project = await _project(db_session, "ARRWIKI")
    if reader == "member_with_view_wiki":
        role = Role(name="ARR wiki reader", permissions=[Permission.VIEW_WIKI, Permission.VIEW_ISSUES])
        db_session.add(role)
        await db_session.flush()
        member = Member(project_id=project.id, user_id=outsider.id)
        db_session.add(member)
        await db_session.flush()
        db_session.add(MemberRole(member_id=member.id, role_id=role.id))
        await db_session.commit()
    elif reader == "non_member_with_anonymous_wiki":
        await set_anonymous_permissions(db_session, project, [Permission.VIEW_WIKI], admin)
        await set_anonymous_access_enabled(db_session, True, admin, confirmed_projects=[project.key])
        await db_session.commit()

    before = await _wiki_row_counts(db_session)

    index_status = await _get(client, outsider, f"/projects/{project.key}/wiki/")
    home_status = await _get(client, outsider, f"/projects/{project.key}/wiki/home/")

    assert index_status in (302, 303, 403, 404)
    assert home_status in (403, 404)
    assert await _wiki_row_counts(db_session) == before


async def test_wiki_get_by_an_editor_still_creates_the_home_page(
    client: AsyncClient, db_session: AsyncSession, outsider: User
) -> None:
    """Positive control: the guard only stops readers."""
    project = await _project(db_session, "ARRWIKIED")
    role = Role(name="ARR wiki editor", permissions=[Permission.VIEW_WIKI, Permission.MANAGE_WIKI])
    db_session.add(role)
    await db_session.flush()
    member = Member(project_id=project.id, user_id=outsider.id)
    db_session.add(member)
    await db_session.flush()
    db_session.add(MemberRole(member_id=member.id, role_id=role.id))
    await db_session.commit()

    assert await _get(client, outsider, f"/projects/{project.key}/wiki/home/") == 200
    assert (await _wiki_row_counts(db_session))[1] >= 1


async def test_non_member_role_is_never_cached_as_membership(
    db_session: AsyncSession, outsider: User, opted_in: Project
) -> None:
    """A non-member's fallback roles carry the seeded role, not a membership row."""
    roles = await get_user_roles(db_session, outsider, opted_in)
    assert [r.builtin for r in roles if not r.transient] == [RoleBuiltin.NON_MEMBER]
    assert [set(r.permissions) for r in roles if r.transient] == [{Permission.VIEW_ISSUES, Permission.VIEW_WIKI}]
