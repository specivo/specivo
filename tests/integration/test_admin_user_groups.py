"""Integration tests for the admin user groups API (``/api/v1/admin/groups/``).

This is an admin surface over a membership principal: a group grants its roles
to every user in it, on every project it is a member of. The tests therefore
cover both the CRUD contract and the access consequences — that the surface is
closed to non-admins, that a delete reports the access it revoked, and that
every mutation leaves an audit trail.

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

from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.role import Role
from specivo.models.security_audit import SecurityAuditLog
from specivo.models.user import User
from specivo.models.user_group import UserGroup, UserGroupMember
from specivo.services.security_audit_service import AuditEvent, GroupAction
from tests.factories.project import ProjectFactory
from tests.factories.user import UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

GROUPS_URL = "/api/v1/admin/groups/"

# Module-local name prefix — see the module docstring.
_PREFIX = "ugapi"
_counter = itertools.count(1)


def _name(stem: str) -> str:
    return f"{_PREFIX}-{stem}-{next(_counter)}"


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def group(db_session: AsyncSession) -> UserGroup:
    obj = UserGroup(name=_name("Developers"), description="Writes code")
    db_session.add(obj)
    await db_session.commit()
    await db_session.refresh(obj)
    return obj


@pytest_asyncio.fixture
async def member_user(db_session: AsyncSession) -> User:
    obj = UserFactory.build()
    db_session.add(obj)
    await db_session.commit()
    await db_session.refresh(obj)
    return obj


async def _make_project(db: AsyncSession) -> Project:
    obj = ProjectFactory.build()
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return obj


async def _grant(db: AsyncSession, group: UserGroup, project: Project, role_names: list[str]) -> None:
    """Give *group* a membership on *project* holding roles named *role_names*."""
    member = Member(group_id=group.id, project_id=project.id)
    db.add(member)
    await db.flush()
    for role_name in role_names:
        role = Role(name=role_name, permissions=["view_issues"], builtin=0, issues_visibility="default")
        db.add(role)
        await db.flush()
        db.add(MemberRole(member_id=member.id, role_id=role.id))
    await db.commit()


async def _group_events(db: AsyncSession, group_id: int) -> list[SecurityAuditLog]:
    """Return the group_change audit rows for one group, oldest first."""
    result = await db.execute(
        select(SecurityAuditLog)
        .where(
            SecurityAuditLog.event_type == AuditEvent.GROUP_CHANGE,
            SecurityAuditLog.resource_id == group_id,
        )
        .order_by(SecurityAuditLog.created_at, SecurityAuditLog.id)
    )
    return list(result.scalars().all())


def _actions(events: list[SecurityAuditLog]) -> list[str]:
    return [e.details["action"] for e in events]


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


class TestCreateGroup:
    async def test_create_returns_201_and_the_group(self, admin_client: AsyncClient):
        name = _name("Backend")
        resp = await admin_client.post(GROUPS_URL, json={"name": name, "description": "Server side"})

        assert resp.status_code == 201
        body = resp.json()
        assert body["name"] == name
        assert body["description"] == "Server side"
        assert body["id"] > 0

    async def test_description_is_optional(self, admin_client: AsyncClient):
        resp = await admin_client.post(GROUPS_URL, json={"name": _name("Minimal")})
        assert resp.status_code == 201
        assert resp.json()["description"] is None

    async def test_name_is_stripped(self, admin_client: AsyncClient):
        name = _name("Padded")
        resp = await admin_client.post(GROUPS_URL, json={"name": f"  {name}  "})
        assert resp.status_code == 201
        assert resp.json()["name"] == name

    async def test_blank_name_is_rejected(self, admin_client: AsyncClient):
        resp = await admin_client.post(GROUPS_URL, json={"name": "   "})
        assert resp.status_code == 422

    async def test_duplicate_name_returns_409(self, admin_client: AsyncClient):
        name = _name("Duplicate")
        assert (await admin_client.post(GROUPS_URL, json={"name": name})).status_code == 201

        resp = await admin_client.post(GROUPS_URL, json={"name": name})
        assert resp.status_code == 409

    async def test_duplicate_name_returns_409_ignoring_case(self, admin_client: AsyncClient):
        name = _name("CaseFold")
        assert (await admin_client.post(GROUPS_URL, json={"name": name})).status_code == 201

        resp = await admin_client.post(GROUPS_URL, json={"name": name.upper()})
        assert resp.status_code == 409
        assert resp.json()["errors"][0]["field"] == "name"

    async def test_create_is_audited(self, admin_client: AsyncClient, db_session: AsyncSession):
        name = _name("Audited")
        resp = await admin_client.post(GROUPS_URL, json={"name": name})
        group_id = resp.json()["id"]

        events = await _group_events(db_session, group_id)

        assert _actions(events) == [GroupAction.CREATED]
        assert events[0].details["group_name"] == name
        assert events[0].user_id == admin_client.state.user.id
        assert events[0].resource_type == "user_group"


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


class TestListGroups:
    async def test_list_returns_a_paginated_envelope(self, admin_client: AsyncClient, group: UserGroup):
        resp = await admin_client.get(GROUPS_URL, params={"q": group.name})

        assert resp.status_code == 200
        body = resp.json()
        assert body["total_count"] == 1
        assert body["offset"] == 0
        assert [item["name"] for item in body["items"]] == [group.name]

    async def test_rows_carry_member_and_project_counts(
        self, admin_client: AsyncClient, db_session: AsyncSession, group: UserGroup, member_user: User
    ):
        db_session.add(UserGroupMember(group_id=group.id, user_id=member_user.id))
        await db_session.commit()
        await _grant(db_session, group, await _make_project(db_session), [_name("Role")])

        resp = await admin_client.get(GROUPS_URL, params={"q": group.name})

        row = resp.json()["items"][0]
        assert row["user_count"] == 1
        assert row["project_count"] == 1

    async def test_search_is_case_insensitive(self, admin_client: AsyncClient, group: UserGroup):
        resp = await admin_client.get(GROUPS_URL, params={"q": group.name.upper()})
        assert [item["id"] for item in resp.json()["items"]] == [group.id]

    async def test_pagination_reports_the_full_total(self, admin_client: AsyncClient):
        stem = f"{_PREFIX}-listpage"
        for index in range(3):
            await admin_client.post(GROUPS_URL, json={"name": f"{stem}-{index}"})

        resp = await admin_client.get(GROUPS_URL, params={"q": stem, "offset": 1, "limit": 1})

        body = resp.json()
        assert body["total_count"] == 3
        assert len(body["items"]) == 1


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------


class TestGroupDetail:
    async def test_detail_returns_the_group(self, admin_client: AsyncClient, group: UserGroup):
        resp = await admin_client.get(f"{GROUPS_URL}{group.id}/")

        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == group.id
        assert body["name"] == group.name
        assert body["projects"] == []
        assert body["user_count"] == 0

    async def test_detail_shows_the_projects_and_roles_the_group_grants(
        self, admin_client: AsyncClient, db_session: AsyncSession, group: UserGroup
    ):
        project = await _make_project(db_session)
        developer, reviewer = _name("Developer"), _name("Reviewer")
        await _grant(db_session, group, project, [developer, reviewer])

        resp = await admin_client.get(f"{GROUPS_URL}{group.id}/")

        projects = resp.json()["projects"]
        assert len(projects) == 1
        assert projects[0]["project_id"] == project.id
        assert projects[0]["key"] == project.key
        assert sorted(projects[0]["roles"]) == sorted([developer, reviewer])

    async def test_detail_counts_the_users(
        self, admin_client: AsyncClient, db_session: AsyncSession, group: UserGroup, member_user: User
    ):
        db_session.add(UserGroupMember(group_id=group.id, user_id=member_user.id))
        await db_session.commit()

        resp = await admin_client.get(f"{GROUPS_URL}{group.id}/")

        assert resp.json()["user_count"] == 1

    async def test_missing_group_returns_404(self, admin_client: AsyncClient):
        resp = await admin_client.get(f"{GROUPS_URL}987654321/")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


class TestUpdateGroup:
    async def test_rename(self, admin_client: AsyncClient, group: UserGroup):
        new_name = _name("Renamed")
        resp = await admin_client.patch(f"{GROUPS_URL}{group.id}/", json={"name": new_name})

        assert resp.status_code == 200
        assert resp.json()["name"] == new_name

    async def test_edit_description_only(self, admin_client: AsyncClient, group: UserGroup):
        resp = await admin_client.patch(f"{GROUPS_URL}{group.id}/", json={"description": "Updated"})

        assert resp.status_code == 200
        assert resp.json()["name"] == group.name
        assert resp.json()["description"] == "Updated"

    async def test_description_can_be_cleared(self, admin_client: AsyncClient, group: UserGroup):
        resp = await admin_client.patch(f"{GROUPS_URL}{group.id}/", json={"description": None})
        assert resp.json()["description"] is None

    async def test_rename_onto_an_existing_name_returns_409(self, admin_client: AsyncClient, group: UserGroup):
        taken = _name("Taken")
        await admin_client.post(GROUPS_URL, json={"name": taken})

        resp = await admin_client.patch(f"{GROUPS_URL}{group.id}/", json={"name": taken})
        assert resp.status_code == 409

    async def test_rename_collision_is_case_insensitive(self, admin_client: AsyncClient, group: UserGroup):
        taken = _name("TakenCased")
        await admin_client.post(GROUPS_URL, json={"name": taken})

        resp = await admin_client.patch(f"{GROUPS_URL}{group.id}/", json={"name": taken.upper()})
        assert resp.status_code == 409

    async def test_blank_name_is_rejected(self, admin_client: AsyncClient, group: UserGroup):
        resp = await admin_client.patch(f"{GROUPS_URL}{group.id}/", json={"name": "  "})
        assert resp.status_code == 422

    async def test_missing_group_returns_404(self, admin_client: AsyncClient):
        resp = await admin_client.patch(f"{GROUPS_URL}987654321/", json={"name": _name("Ghost")})
        assert resp.status_code == 404

    async def test_rename_is_audited_with_the_old_name(
        self, admin_client: AsyncClient, db_session: AsyncSession, group: UserGroup
    ):
        old_name, new_name = group.name, _name("AuditedRename")
        await admin_client.patch(f"{GROUPS_URL}{group.id}/", json={"name": new_name})

        events = await _group_events(db_session, group.id)

        assert _actions(events) == [GroupAction.RENAMED]
        assert events[0].details["old_name"] == old_name
        assert events[0].details["group_name"] == new_name

    async def test_a_description_only_edit_is_not_a_rename(
        self, admin_client: AsyncClient, db_session: AsyncSession, group: UserGroup
    ):
        await admin_client.patch(f"{GROUPS_URL}{group.id}/", json={"description": "Same name"})

        assert await _group_events(db_session, group.id) == []


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


class TestDeleteGroup:
    async def test_delete_returns_204(self, admin_client: AsyncClient, group: UserGroup):
        resp = await admin_client.delete(f"{GROUPS_URL}{group.id}/")

        assert resp.status_code == 204
        assert (await admin_client.get(f"{GROUPS_URL}{group.id}/")).status_code == 404

    async def test_delete_removes_the_project_memberships(
        self, admin_client: AsyncClient, db_session: AsyncSession, group: UserGroup, member_user: User
    ):
        project = await _make_project(db_session)
        db_session.add(UserGroupMember(group_id=group.id, user_id=member_user.id))
        await db_session.commit()
        await _grant(db_session, group, project, [_name("Role")])
        group_id = group.id

        assert (await admin_client.delete(f"{GROUPS_URL}{group_id}/")).status_code == 204

        remaining = await db_session.execute(select(Member.id).where(Member.group_id == group_id))
        assert remaining.scalars().all() == []
        links = await db_session.execute(select(UserGroupMember.id).where(UserGroupMember.group_id == group_id))
        assert links.scalars().all() == []

    async def test_delete_reports_what_it_removed(
        self, admin_client: AsyncClient, db_session: AsyncSession, group: UserGroup, member_user: User
    ):
        db_session.add(UserGroupMember(group_id=group.id, user_id=member_user.id))
        await db_session.commit()
        await _grant(db_session, group, await _make_project(db_session), [_name("Role")])
        await _grant(db_session, group, await _make_project(db_session), [_name("Role")])

        resp = await admin_client.delete(f"{GROUPS_URL}{group.id}/")

        assert resp.headers["X-Removed-Users"] == "1"
        assert resp.headers["X-Removed-Project-Memberships"] == "2"

    async def test_delete_reports_zeroes_for_an_empty_group(self, admin_client: AsyncClient, group: UserGroup):
        resp = await admin_client.delete(f"{GROUPS_URL}{group.id}/")

        assert resp.headers["X-Removed-Users"] == "0"
        assert resp.headers["X-Removed-Project-Memberships"] == "0"

    async def test_missing_group_returns_404(self, admin_client: AsyncClient):
        resp = await admin_client.delete(f"{GROUPS_URL}987654321/")
        assert resp.status_code == 404

    async def test_delete_is_audited_with_what_the_group_was_granting(
        self, admin_client: AsyncClient, db_session: AsyncSession, group: UserGroup, member_user: User
    ):
        db_session.add(UserGroupMember(group_id=group.id, user_id=member_user.id))
        await db_session.commit()
        await _grant(db_session, group, await _make_project(db_session), [_name("Role")])
        group_id, group_name = group.id, group.name

        await admin_client.delete(f"{GROUPS_URL}{group_id}/")

        events = await _group_events(db_session, group_id)

        assert _actions(events) == [GroupAction.DELETED]
        assert events[0].details["group_name"] == group_name
        assert events[0].details["users_removed"] == 1
        assert events[0].details["project_memberships_removed"] == 1


# ---------------------------------------------------------------------------
# Users in a group
# ---------------------------------------------------------------------------


class TestGroupUsers:
    async def test_add_user_returns_201(self, admin_client: AsyncClient, group: UserGroup, member_user: User):
        resp = await admin_client.post(f"{GROUPS_URL}{group.id}/users/", json={"user_id": member_user.id})

        assert resp.status_code == 201
        assert resp.json()["user_id"] == member_user.id
        assert resp.json()["login"] == member_user.login

    async def test_list_users(self, admin_client: AsyncClient, group: UserGroup, member_user: User):
        await admin_client.post(f"{GROUPS_URL}{group.id}/users/", json={"user_id": member_user.id})

        resp = await admin_client.get(f"{GROUPS_URL}{group.id}/users/")

        assert resp.status_code == 200
        body = resp.json()
        assert body["total_count"] == 1
        assert [item["user_id"] for item in body["items"]] == [member_user.id]

    async def test_adding_the_same_user_twice_is_a_no_op(
        self, admin_client: AsyncClient, group: UserGroup, member_user: User
    ):
        first = await admin_client.post(f"{GROUPS_URL}{group.id}/users/", json={"user_id": member_user.id})
        second = await admin_client.post(f"{GROUPS_URL}{group.id}/users/", json={"user_id": member_user.id})

        assert first.status_code == 201
        assert second.status_code == 201

        listing = await admin_client.get(f"{GROUPS_URL}{group.id}/users/")
        assert listing.json()["total_count"] == 1

    async def test_adding_a_missing_user_returns_404(self, admin_client: AsyncClient, group: UserGroup):
        resp = await admin_client.post(f"{GROUPS_URL}{group.id}/users/", json={"user_id": 987654321})
        assert resp.status_code == 404

    async def test_adding_to_a_missing_group_returns_404(self, admin_client: AsyncClient, member_user: User):
        resp = await admin_client.post(f"{GROUPS_URL}987654321/users/", json={"user_id": member_user.id})
        assert resp.status_code == 404

    async def test_remove_user_returns_204(self, admin_client: AsyncClient, group: UserGroup, member_user: User):
        await admin_client.post(f"{GROUPS_URL}{group.id}/users/", json={"user_id": member_user.id})

        resp = await admin_client.delete(f"{GROUPS_URL}{group.id}/users/{member_user.id}/")

        assert resp.status_code == 204
        listing = await admin_client.get(f"{GROUPS_URL}{group.id}/users/")
        assert listing.json()["total_count"] == 0

    async def test_removing_a_non_member_returns_404(
        self, admin_client: AsyncClient, group: UserGroup, member_user: User
    ):
        resp = await admin_client.delete(f"{GROUPS_URL}{group.id}/users/{member_user.id}/")
        assert resp.status_code == 404

    async def test_listing_users_of_a_missing_group_returns_404(self, admin_client: AsyncClient):
        resp = await admin_client.get(f"{GROUPS_URL}987654321/users/")
        assert resp.status_code == 404

    async def test_adding_and_removing_a_user_is_audited(
        self, admin_client: AsyncClient, db_session: AsyncSession, group: UserGroup, member_user: User
    ):
        await admin_client.post(f"{GROUPS_URL}{group.id}/users/", json={"user_id": member_user.id})
        await admin_client.delete(f"{GROUPS_URL}{group.id}/users/{member_user.id}/")

        events = await _group_events(db_session, group.id)

        assert _actions(events) == [GroupAction.USER_ADDED, GroupAction.USER_REMOVED]
        assert all(e.details["target_user_id"] == member_user.id for e in events)
        assert all(e.details["target_login"] == member_user.login for e in events)

    async def test_a_repeated_add_writes_no_second_audit_row(
        self, admin_client: AsyncClient, db_session: AsyncSession, group: UserGroup, member_user: User
    ):
        """Nothing changed, so nothing is logged."""
        await admin_client.post(f"{GROUPS_URL}{group.id}/users/", json={"user_id": member_user.id})
        await admin_client.post(f"{GROUPS_URL}{group.id}/users/", json={"user_id": member_user.id})

        assert _actions(await _group_events(db_session, group.id)) == [GroupAction.USER_ADDED]


# ---------------------------------------------------------------------------
# The surface is admin-only
# ---------------------------------------------------------------------------


class TestNonAdminIsRefused:
    """Every route on this router must be closed to a non-admin.

    The paths are exercised against a real group so a 403 cannot be confused
    with a 404 from a missing row.
    """

    async def test_list_is_refused(self, auth_client: AsyncClient):
        assert (await auth_client.get(GROUPS_URL)).status_code == 403

    async def test_create_is_refused(self, auth_client: AsyncClient):
        resp = await auth_client.post(GROUPS_URL, json={"name": _name("Forbidden")})
        assert resp.status_code == 403

    async def test_detail_is_refused(self, auth_client: AsyncClient, group: UserGroup):
        assert (await auth_client.get(f"{GROUPS_URL}{group.id}/")).status_code == 403

    async def test_update_is_refused(self, auth_client: AsyncClient, group: UserGroup):
        resp = await auth_client.patch(f"{GROUPS_URL}{group.id}/", json={"name": _name("Forbidden")})
        assert resp.status_code == 403

    async def test_delete_is_refused(self, auth_client: AsyncClient, group: UserGroup):
        assert (await auth_client.delete(f"{GROUPS_URL}{group.id}/")).status_code == 403

    async def test_list_users_is_refused(self, auth_client: AsyncClient, group: UserGroup):
        assert (await auth_client.get(f"{GROUPS_URL}{group.id}/users/")).status_code == 403

    async def test_add_user_is_refused(self, auth_client: AsyncClient, group: UserGroup, member_user: User):
        resp = await auth_client.post(f"{GROUPS_URL}{group.id}/users/", json={"user_id": member_user.id})
        assert resp.status_code == 403

    async def test_remove_user_is_refused(self, auth_client: AsyncClient, group: UserGroup, member_user: User):
        resp = await auth_client.delete(f"{GROUPS_URL}{group.id}/users/{member_user.id}/")
        assert resp.status_code == 403

    async def test_a_refused_request_changes_nothing(
        self, auth_client: AsyncClient, db_session: AsyncSession, group: UserGroup
    ):
        await auth_client.delete(f"{GROUPS_URL}{group.id}/")

        still_there = await db_session.execute(select(UserGroup.id).where(UserGroup.id == group.id))
        assert still_there.scalar_one_or_none() == group.id

    async def test_unauthenticated_is_refused(self, client: AsyncClient):
        assert (await client.get(GROUPS_URL)).status_code == 401
