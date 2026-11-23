"""Integration tests for the admin roles API (``/api/v1/admin/roles/``).

Covers who may reach it, the CRUD contract with its error codes and envelope,
the permission catalogue, and the audit trail (including that a PATCH that
changes nothing writes no event).

Role names are unique database-wide and modules run in parallel inside
uncommitted transactions, so names here carry a module-local prefix.
"""

from __future__ import annotations

import itertools

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.member import Member, MemberRole
from specivo.models.role import Role, RoleBuiltin
from specivo.models.security_audit import SecurityAuditLog
from specivo.services.api_key_service import ApiKeyService
from specivo.services.security_audit_service import AuditEvent
from tests.factories.project import ProjectFactory
from tests.factories.user import UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

URL = "/api/v1/admin/roles/"
_PREFIX = "rapi"
_counter = itertools.count(1)


def _name(stem: str) -> str:
    return f"{_PREFIX}-{stem}-{next(_counter)}"


def _error(resp: Response) -> dict:
    body = resp.json()
    assert list(body) == ["errors"]
    return body["errors"][0]


async def _role(db: AsyncSession, permissions: list[str] | None = None) -> Role:
    role = Role(name=_name("Role"), permissions=permissions or ["view_issues"], builtin=0)
    db.add(role)
    await db.commit()
    await db.refresh(role)
    return role


async def _events(db: AsyncSession, role_id: int) -> list[SecurityAuditLog]:
    result = await db.execute(
        select(SecurityAuditLog)
        .where(SecurityAuditLog.resource_type == "role", SecurityAuditLog.resource_id == role_id)
        .order_by(SecurityAuditLog.id)
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------


async def test_anonymous_is_unauthorized(client: AsyncClient) -> None:
    assert (await client.get(URL)).status_code == 401
    assert (await client.post(URL, json={"name": _name("Anon")})).status_code == 401


async def test_non_admin_is_forbidden(auth_client: AsyncClient, db_session: AsyncSession) -> None:
    role = await _role(db_session)
    assert (await auth_client.get(URL)).status_code == 403
    assert (await auth_client.get(f"{URL}permissions/")).status_code == 403
    assert (await auth_client.post(URL, json={"name": _name("Nope")})).status_code == 403
    resp = await auth_client.patch(f"{URL}{role.id}/", json={"lock_version": 0, "name": _name("Nope")})
    assert resp.status_code == 403
    assert (await auth_client.delete(f"{URL}{role.id}/")).status_code == 403


async def test_project_limited_admin_key_is_forbidden(admin_client: AsyncClient, db_session: AsyncSession) -> None:
    project = ProjectFactory.build(key="RAPILIM", identifier="rapi-lim")
    db_session.add(project)
    await db_session.commit()
    _key, raw = await ApiKeyService().create_key(
        session=db_session,
        user_id=admin_client.state.user.id,
        name="roles-limited",
        scopes={"projects": [project.key]},
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {raw}"}
    assert (await admin_client.get(URL, headers=headers)).status_code == 403


async def test_admin_api_key_is_allowed(admin_client: AsyncClient, db_session: AsyncSession) -> None:
    _key, raw = await ApiKeyService().create_key(
        session=db_session, user_id=admin_client.state.user.id, name="roles-full"
    )
    await db_session.commit()
    resp = await admin_client.get(URL, headers={"Authorization": f"Bearer {raw}"})
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def test_list_roles(admin_client: AsyncClient, db_session: AsyncSession) -> None:
    role = await _role(db_session, permissions=["*"])
    project = ProjectFactory.build(key="RAPILST", identifier="rapi-lst")
    user = UserFactory.build(login=_name("member"))
    db_session.add_all([project, user])
    await db_session.flush()
    member = Member(user_id=user.id, project_id=project.id)
    db_session.add(member)
    await db_session.flush()
    db_session.add(MemberRole(member_id=member.id, role_id=role.id))
    await db_session.commit()

    resp = await admin_client.get(URL, params={"limit": 200})
    assert resp.status_code == 200
    body = resp.json()
    items = body["items"]
    assert body["total_count"] == len(items)
    assert items[-1]["builtin"] == RoleBuiltin.NON_MEMBER
    row = next(i for i in items if i["id"] == role.id)
    assert row["full_access"] is True
    assert row["membership_count"] == 1
    assert row["system_key"] is None
    assert set(row) == {
        "id",
        "name",
        "position",
        "assignable",
        "builtin",
        "system_key",
        "full_access",
        "permissions",
        "issues_visibility",
        "users_visibility",
        "time_entries_visibility",
        "ai_usage_visibility",
        "lock_version",
        "membership_count",
        "created_at",
        "updated_at",
    }
    automation = next(i for i in items if i["system_key"] == "automation")
    assert automation["builtin"] == 0


async def test_list_pagination(admin_client: AsyncClient) -> None:
    resp = await admin_client.get(URL, params={"offset": 0, "limit": 1})
    body = resp.json()
    assert len(body["items"]) == 1
    assert body["total_count"] >= 2


async def test_catalogue(admin_client: AsyncClient) -> None:
    resp = await admin_client.get(f"{URL}permissions/")
    assert resp.status_code == 200
    body = resp.json()
    # Every permission is listed; the total counts only the enforced ones.
    assert sum(len(g["permissions"]) for g in body["groups"]) == 28
    assert body["total"] == 25
    wiki = next(g for g in body["groups"] if g["key"] == "wiki")
    delete = next(p for p in wiki["permissions"] if p["key"] == "delete_wiki_pages")
    assert delete["requires"] == ["view_wiki", "manage_wiki"]
    assert "manage_members" not in body["non_member_allowed"]


async def test_get_role_detail(admin_client: AsyncClient, db_session: AsyncSession) -> None:
    role = await _role(db_session)
    resp = await admin_client.get(f"{URL}{role.id}/")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == role.id
    assert body["projects"] == []
    assert body["workflow_transition_count"] == 0
    assert body["field_rule_count"] == 0


async def test_get_unknown_role_is_404(admin_client: AsyncClient) -> None:
    resp = await admin_client.get(f"{URL}987654321/")
    assert resp.status_code == 404
    assert _error(resp)["code"] == "not_found"


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def test_create_blank_and_audit(admin_client: AsyncClient, db_session: AsyncSession) -> None:
    name = _name("Blank")
    resp = await admin_client.post(URL, json={"name": name, "permissions": ["log_time"]})
    assert resp.status_code == 201
    body = resp.json()
    assert body["permissions"] == ["view_time_entries", "log_time"]
    assert body["membership_count"] == 0

    [event] = await _events(db_session, body["id"])
    assert event.event_type == AuditEvent.ROLE_CREATED
    assert event.user_id == admin_client.state.user.id
    assert event.details == {
        "role_name": name,
        "permissions": ["view_time_entries", "log_time"],
        "copied_from": None,
        "workflow_transitions_copied": 0,
        "field_rules_copied": 0,
    }


async def test_create_copy(admin_client: AsyncClient, db_session: AsyncSession) -> None:
    source = await _role(db_session, permissions=["view_issues", "add_issues"])
    resp = await admin_client.post(URL, json={"name": _name("Copy"), "copy_from_role_id": source.id})
    assert resp.status_code == 201
    assert resp.json()["permissions"] == ["view_issues", "add_issues"]


async def test_create_copy_from_unknown_is_404(admin_client: AsyncClient) -> None:
    resp = await admin_client.post(URL, json={"name": _name("Copy"), "copy_from_role_id": 987654321})
    assert resp.status_code == 404


async def test_create_copy_with_permissions_is_422(admin_client: AsyncClient, db_session: AsyncSession) -> None:
    source = await _role(db_session)
    resp = await admin_client.post(
        URL, json={"name": _name("Both"), "copy_from_role_id": source.id, "permissions": ["view_issues"]}
    )
    assert resp.status_code == 422
    assert _error(resp)["code"] == "validation_error"


async def test_create_name_taken_is_409(admin_client: AsyncClient, db_session: AsyncSession) -> None:
    role = await _role(db_session)
    resp = await admin_client.post(URL, json={"name": role.name.upper()})
    assert resp.status_code == 409
    error = _error(resp)
    assert error["code"] == "role_name_taken"
    assert error["field"] == "name"


async def test_create_unknown_permission_is_422(admin_client: AsyncClient) -> None:
    resp = await admin_client.post(URL, json={"name": _name("Bad"), "permissions": ["fly"]})
    assert resp.status_code == 422
    error = _error(resp)
    assert (error["code"], error["field"]) == ("validation_error", "permissions")


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


async def test_patch_and_audit_diff(admin_client: AsyncClient, db_session: AsyncSession) -> None:
    role = await _role(db_session, permissions=["view_issues"])
    resp = await admin_client.patch(
        f"{URL}{role.id}/",
        json={"lock_version": role.lock_version, "permissions": ["view_issues", "edit_issues"], "assignable": False},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["permissions"] == ["view_issues", "edit_issues"]
    assert body["lock_version"] == role.lock_version + 1

    [event] = await _events(db_session, role.id)
    assert event.event_type == AuditEvent.ROLE_UPDATED
    assert event.details == {
        "role_name": role.name,
        "assignable": {"old": True, "new": False},
        "permissions": {"added": ["edit_issues"], "removed": []},
    }


async def test_noop_patch_writes_no_event(admin_client: AsyncClient, db_session: AsyncSession) -> None:
    role = await _role(db_session, permissions=["view_issues"])
    resp = await admin_client.patch(
        f"{URL}{role.id}/", json={"lock_version": role.lock_version, "permissions": ["view_issues"]}
    )
    assert resp.status_code == 200
    assert resp.json()["lock_version"] == role.lock_version
    assert await _events(db_session, role.id) == []


async def test_patch_stale_version_is_409(admin_client: AsyncClient, db_session: AsyncSession) -> None:
    role = await _role(db_session)
    resp = await admin_client.patch(f"{URL}{role.id}/", json={"lock_version": 99, "name": _name("Late")})
    assert resp.status_code == 409
    assert _error(resp)["code"] == "conflict_lock_version"


async def test_patch_explicit_null_is_422(admin_client: AsyncClient, db_session: AsyncSession) -> None:
    role = await _role(db_session)
    resp = await admin_client.patch(f"{URL}{role.id}/", json={"lock_version": role.lock_version, "name": None})
    assert resp.status_code == 422


async def test_patch_without_lock_version_is_422(admin_client: AsyncClient, db_session: AsyncSession) -> None:
    role = await _role(db_session)
    resp = await admin_client.patch(f"{URL}{role.id}/", json={"name": _name("NoVersion")})
    assert resp.status_code == 422


async def test_patch_bad_visibility_is_422(admin_client: AsyncClient, db_session: AsyncSession) -> None:
    role = await _role(db_session)
    resp = await admin_client.patch(
        f"{URL}{role.id}/", json={"lock_version": role.lock_version, "time_entries_visibility": "everyone"}
    )
    assert resp.status_code == 422


async def test_patch_non_member_ceiling_is_422(admin_client: AsyncClient, db_session: AsyncSession) -> None:
    non_member = (await db_session.execute(select(Role).where(Role.builtin == RoleBuiltin.NON_MEMBER))).scalar_one()
    resp = await admin_client.patch(
        f"{URL}{non_member.id}/",
        json={"lock_version": non_member.lock_version, "permissions": ["view_issues", "manage_members"]},
    )
    assert resp.status_code == 422
    error = _error(resp)
    assert error["code"] == "role_permission_not_allowed"
    assert error["field"] == "permissions"
    assert error["details"]["not_allowed"] == ["manage_members"]


# ---------------------------------------------------------------------------
# Move
# ---------------------------------------------------------------------------


async def test_move(admin_client: AsyncClient, db_session: AsyncSession) -> None:
    a = await _role(db_session)
    b = await _role(db_session)
    a.position = b.position = 70_000
    await db_session.commit()

    resp = await admin_client.post(f"{URL}{b.id}/move/", json={"direction": "up"})
    assert resp.status_code == 200
    ids = [r["id"] for r in resp.json()]
    assert ids[-2:] == [b.id, a.id]
    assert all(r["builtin"] == 0 for r in resp.json())


async def test_move_bad_direction_is_422(admin_client: AsyncClient, db_session: AsyncSession) -> None:
    role = await _role(db_session)
    resp = await admin_client.post(f"{URL}{role.id}/move/", json={"direction": "sideways"})
    assert resp.status_code == 422


async def test_move_unknown_is_404(admin_client: AsyncClient) -> None:
    resp = await admin_client.post(f"{URL}987654321/move/", json={"direction": "up"})
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


async def test_delete_and_audit(admin_client: AsyncClient, db_session: AsyncSession) -> None:
    role = await _role(db_session, permissions=["view_wiki"])
    resp = await admin_client.delete(f"{URL}{role.id}/")
    assert resp.status_code == 204
    assert (await admin_client.get(f"{URL}{role.id}/")).status_code == 404

    [event] = await _events(db_session, role.id)
    assert event.event_type == AuditEvent.ROLE_DELETED
    assert event.details == {
        "role_name": role.name,
        "permissions": ["view_wiki"],
        "workflow_transitions_removed": 0,
        "field_rules_removed": 0,
    }


async def test_delete_in_use_is_409(admin_client: AsyncClient, db_session: AsyncSession) -> None:
    role = await _role(db_session)
    project = ProjectFactory.build(key="RAPIUSE", identifier="rapi-use")
    user = UserFactory.build(login=_name("user"))
    db_session.add_all([project, user])
    await db_session.flush()
    member = Member(user_id=user.id, project_id=project.id)
    db_session.add(member)
    await db_session.flush()
    db_session.add(MemberRole(member_id=member.id, role_id=role.id))
    await db_session.commit()

    resp = await admin_client.delete(f"{URL}{role.id}/")
    assert resp.status_code == 409
    error = _error(resp)
    assert error["code"] == "role_in_use"
    assert error["details"]["membership_count"] == 1
    assert error["details"]["projects"] == [{"id": project.id, "key": "RAPIUSE", "name": project.name}]


@pytest.mark.parametrize(("where", "reason"), [("builtin", "builtin"), ("system", "automation")])
async def test_delete_protected_is_409(
    admin_client: AsyncClient, db_session: AsyncSession, where: str, reason: str
) -> None:
    clause = Role.builtin == RoleBuiltin.NON_MEMBER if where == "builtin" else Role.system_key == "automation"
    role = (await db_session.execute(select(Role).where(clause))).scalar_one()
    resp = await admin_client.delete(f"{URL}{role.id}/")
    assert resp.status_code == 409
    error = _error(resp)
    assert error["code"] == "role_protected"
    assert error["details"]["reason"] == reason
