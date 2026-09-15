"""Per-project anonymous permissions: ``projects.anonymous_permissions``.

Covers the schema guarantees (the two CHECKs and the partial index), that only
instance administrators can read or change the list, that making a project
private clears it, that every change is audited with the old and new value,
and that the member-facing project schemas never expose it.

Whether the value changes what anybody can see is covered separately, in
``test_anonymous_access_inert.py``.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.core.exceptions import PermissionDeniedError
from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.role import Role
from specivo.models.security_audit import SecurityAuditLog
from specivo.models.user import User
from specivo.schemas.project import ProjectCreate, ProjectOut, ProjectUpdate
from specivo.services.anonymous_access_service import (
    list_projects_with_anonymous_permissions,
    set_anonymous_permissions,
)
from specivo.services.auth_service import _make_access_token
from specivo.services.permission_service import clear_role_cache
from specivo.services.security_audit_service import AuditEvent
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

URL = "/api/v1/admin/projects/{key}/anonymous-permissions/"


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(autouse=True)
async def _fresh_role_cache():
    clear_role_cache()
    yield
    clear_role_cache()


@pytest_asyncio.fixture
async def admin(db_session: AsyncSession) -> User:
    user = AdminUserFactory.build(login="anonperm_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    return user


@pytest_asyncio.fixture
async def public_project(db_session: AsyncSession) -> Project:
    proj = ProjectFactory.build(key="APUB", identifier="anonperm-public", name="Anonperm Public", is_public=True)
    db_session.add(proj)
    await db_session.commit()
    return proj


@pytest_asyncio.fixture
async def private_project(db_session: AsyncSession) -> Project:
    proj = ProjectFactory.build(key="APRIV", identifier="anonperm-private", name="Anonperm Private", is_public=False)
    db_session.add(proj)
    await db_session.commit()
    return proj


@pytest_asyncio.fixture
async def manager(db_session: AsyncSession, public_project: Project, private_project: Project) -> User:
    """A non-admin holding every permission, manage_project included, on both projects."""
    user = UserFactory.build(login="anonperm_manager", status="active")
    db_session.add(user)
    await db_session.flush()
    role = Role(name="Anonperm Manager", permissions=["*"], builtin=0, issues_visibility="all")
    db_session.add(role)
    await db_session.flush()
    for project in (public_project, private_project):
        member = Member(user_id=user.id, project_id=project.id)
        db_session.add(member)
        await db_session.flush()
        db_session.add(MemberRole(member_id=member.id, role_id=role.id))
    await db_session.commit()
    return user


def _bearer(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {_make_access_token(user, get_settings())}"}


def _cookies(user: User) -> dict[str, str]:
    return {"access_token": _make_access_token(user, get_settings())}


async def _audit_rows(db: AsyncSession, project: Project) -> list[SecurityAuditLog]:
    result = await db.execute(
        select(SecurityAuditLog)
        .where(
            SecurityAuditLog.event_type == AuditEvent.PROJECT_ANONYMOUS_PERMISSIONS_CHANGED,
            SecurityAuditLog.project_id == project.id,
        )
        .order_by(SecurityAuditLog.id)
    )
    return list(result.scalars().all())


async def _stored(db: AsyncSession, project: Project) -> list[str]:
    return list(await db.scalar(text("SELECT anonymous_permissions FROM projects WHERE id = :id"), {"id": project.id}))


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


async def test_new_projects_default_to_no_anonymous_permissions(
    db_session: AsyncSession, private_project: Project
) -> None:
    column_default = await db_session.scalar(
        text(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_name = 'projects' AND column_name = 'anonymous_permissions'"
        )
    )
    await db_session.execute(
        text(
            "INSERT INTO projects (name, identifier, key, path) "
            "VALUES ('Anonperm Raw', 'anonperm-raw', 'APRAW', 'anonperm_raw')"
        )
    )
    raw_value = await db_session.scalar(text("SELECT anonymous_permissions FROM projects WHERE key = 'APRAW'"))

    assert "'[]'::jsonb" in column_default
    assert raw_value == []
    assert private_project.anonymous_permissions == []


async def test_database_objects_match_the_model(db_session: AsyncSession) -> None:
    constraints = set(
        (
            await db_session.execute(
                text("SELECT conname FROM pg_constraint WHERE conrelid = 'projects'::regclass AND contype = 'c'")
            )
        ).scalars()
    )
    index_def = await db_session.scalar(
        text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_projects_anonymous_readable'")
    )
    model_names = {c.name for c in Project.__table__.constraints} | {i.name for i in Project.__table__.indexes}
    expected = {"ck_projects_anonymous_permissions_allowed", "ck_projects_anonymous_permissions_public"}

    assert expected <= constraints
    assert index_def is not None and "WHERE (anonymous_permissions <> '[]'::jsonb)" in index_def
    assert expected | {"ix_projects_anonymous_readable"} <= model_names


@pytest.mark.parametrize(
    "value",
    [
        '["edit_issues"]',
        '["view_issues", "add_issues"]',
        '["*"]',
        '"view_issues"',
        '{"view_issues": true}',
        '[["view_issues"]]',
        "null",
    ],
)
async def test_allowed_check_rejects_anything_beyond_view_issues_and_view_wiki(
    db_session: AsyncSession, public_project: Project, value: str
) -> None:
    with pytest.raises(IntegrityError, match="ck_projects_anonymous_permissions_allowed|not-null|null value"):
        await db_session.execute(
            text("UPDATE projects SET anonymous_permissions = CAST(:v AS jsonb) WHERE id = :id"),
            {"v": value, "id": public_project.id},
        )


@pytest.mark.parametrize("value", ["[]", '["view_issues"]', '["view_wiki"]', '["view_issues", "view_wiki"]'])
async def test_allowed_check_accepts_the_read_permissions_on_a_public_project(
    db_session: AsyncSession, public_project: Project, value: str
) -> None:
    await db_session.execute(
        text("UPDATE projects SET anonymous_permissions = CAST(:v AS jsonb) WHERE id = :id"),
        {"v": value, "id": public_project.id},
    )


async def test_public_check_rejects_permissions_on_a_private_project(
    db_session: AsyncSession, private_project: Project
) -> None:
    with pytest.raises(IntegrityError, match="ck_projects_anonymous_permissions_public"):
        await db_session.execute(
            text("""UPDATE projects SET anonymous_permissions = '["view_issues"]' WHERE id = :id"""),
            {"id": private_project.id},
        )


async def test_public_check_rejects_making_an_opted_in_project_private_behind_the_services_back(
    db_session: AsyncSession, public_project: Project
) -> None:
    await db_session.execute(
        text("""UPDATE projects SET anonymous_permissions = '["view_wiki"]' WHERE id = :id"""),
        {"id": public_project.id},
    )
    with pytest.raises(IntegrityError, match="ck_projects_anonymous_permissions_public"):
        await db_session.execute(
            text("UPDATE projects SET is_public = false WHERE id = :id"), {"id": public_project.id}
        )


async def test_listing_returns_only_opted_in_projects(
    db_session: AsyncSession, admin: User, public_project: Project, private_project: Project
) -> None:
    other = ProjectFactory.build(key="APOTHER", identifier="anonperm-other", name="Anonperm Other", is_public=True)
    db_session.add(other)
    await db_session.flush()
    await set_anonymous_permissions(db_session, public_project, ["view_wiki"], admin)

    listed = await list_projects_with_anonymous_permissions(db_session)

    assert [p.key for p in listed if p.key.startswith("AP")] == ["APUB"]


# ---------------------------------------------------------------------------
# Admin API
# ---------------------------------------------------------------------------


async def test_admin_sets_permissions_and_the_change_is_audited(
    client: AsyncClient, db_session: AsyncSession, admin: User, public_project: Project
) -> None:
    resp = await client.patch(
        URL.format(key="APUB"),
        json={"anonymous_permissions": ["view_wiki", "view_issues", "view_wiki"]},
        headers=_bearer(admin),
    )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"key": "APUB", "is_public": True, "anonymous_permissions": ["view_issues", "view_wiki"]}
    assert await _stored(db_session, public_project) == ["view_issues", "view_wiki"]

    rows = await _audit_rows(db_session, public_project)
    assert len(rows) == 1
    assert rows[0].user_id == admin.id
    assert rows[0].resource_type == "project"
    assert rows[0].details == {
        "project_key": "APUB",
        "old": [],
        "new": ["view_issues", "view_wiki"],
        "reason": "admin_update",
    }


async def test_admin_reads_permissions(client: AsyncClient, db_session: AsyncSession, admin: User, public_project):
    await set_anonymous_permissions(db_session, public_project, ["view_issues"], admin)
    await db_session.commit()

    resp = await client.get(URL.format(key="apub"), headers=_bearer(admin))

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"key": "APUB", "is_public": True, "anonymous_permissions": ["view_issues"]}


async def test_each_change_is_audited_with_old_and_new_value(
    client: AsyncClient, db_session: AsyncSession, admin: User, public_project: Project
) -> None:
    for value in (["view_issues"], ["view_wiki"], []):
        resp = await client.patch(URL.format(key="APUB"), json={"anonymous_permissions": value}, headers=_bearer(admin))
        assert resp.status_code == 200, resp.text

    rows = await _audit_rows(db_session, public_project)
    assert [(r.details["old"], r.details["new"]) for r in rows] == [
        ([], ["view_issues"]),
        (["view_issues"], ["view_wiki"]),
        (["view_wiki"], []),
    ]


async def test_saving_the_same_value_writes_no_audit_entry(
    client: AsyncClient, db_session: AsyncSession, admin: User, public_project: Project
) -> None:
    for _ in range(2):
        resp = await client.patch(
            URL.format(key="APUB"), json={"anonymous_permissions": ["view_wiki"]}, headers=_bearer(admin)
        )
        assert resp.status_code == 200, resp.text

    assert len(await _audit_rows(db_session, public_project)) == 1


@pytest.mark.parametrize("value", [["edit_issues"], ["view_issues", "*"], ["view_wiki_edits"]])
async def test_admin_cannot_grant_anything_else(
    client: AsyncClient, db_session: AsyncSession, admin: User, public_project: Project, value: list[str]
) -> None:
    resp = await client.patch(URL.format(key="APUB"), json={"anonymous_permissions": value}, headers=_bearer(admin))

    assert resp.status_code == 422, resp.text
    assert await _stored(db_session, public_project) == []
    assert await _audit_rows(db_session, public_project) == []


async def test_admin_cannot_open_a_private_project(
    client: AsyncClient, db_session: AsyncSession, admin: User, private_project: Project
) -> None:
    resp = await client.patch(
        URL.format(key="APRIV"), json={"anonymous_permissions": ["view_issues"]}, headers=_bearer(admin)
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["errors"][0]["code"] == "project_not_public"
    assert await _stored(db_session, private_project) == []


async def test_unknown_project_is_404(client: AsyncClient, admin: User) -> None:
    resp = await client.patch(URL.format(key="NOPE"), json={"anonymous_permissions": []}, headers=_bearer(admin))

    assert resp.status_code == 404


async def test_project_manager_can_neither_read_nor_change_it(
    client: AsyncClient, db_session: AsyncSession, manager: User, public_project: Project
) -> None:
    get_resp = await client.get(URL.format(key="APUB"), headers=_bearer(manager))
    patch_resp = await client.patch(
        URL.format(key="APUB"), json={"anonymous_permissions": ["view_issues"]}, headers=_bearer(manager)
    )

    assert get_resp.status_code == 403
    assert patch_resp.status_code == 403
    assert await _stored(db_session, public_project) == []
    assert await _audit_rows(db_session, public_project) == []


async def test_service_refuses_a_project_manager(
    db_session: AsyncSession, manager: User, public_project: Project
) -> None:
    with pytest.raises(PermissionDeniedError):
        await set_anonymous_permissions(db_session, public_project, ["view_issues"], manager)


async def test_unauthenticated_request_is_refused(client: AsyncClient, public_project: Project) -> None:
    resp = await client.patch(URL.format(key="APUB"), json={"anonymous_permissions": ["view_issues"]})

    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Member-facing project API
# ---------------------------------------------------------------------------


async def test_member_facing_schemas_do_not_carry_the_field() -> None:
    for schema in (ProjectCreate, ProjectUpdate, ProjectOut):
        assert "anonymous_permissions" not in schema.model_fields


async def test_member_facing_responses_do_not_expose_it(
    client: AsyncClient, db_session: AsyncSession, admin: User, public_project: Project
) -> None:
    await set_anonymous_permissions(db_session, public_project, ["view_issues"], admin)
    await db_session.commit()

    detail = await client.get("/api/v1/projects/APUB/", headers=_bearer(admin))
    listing = await client.get("/api/v1/projects/", headers=_bearer(admin))

    assert detail.status_code == 200 and listing.status_code == 200
    assert "anonymous_permissions" not in detail.text
    assert "anonymous_permissions" not in listing.text


async def test_project_patch_ignores_the_field(
    client: AsyncClient, db_session: AsyncSession, manager: User, public_project: Project
) -> None:
    resp = await client.patch(
        "/api/v1/projects/APUB/",
        json={"name": "Anonperm Renamed", "anonymous_permissions": ["view_issues", "view_wiki"]},
        headers=_bearer(manager),
    )

    assert resp.status_code == 200, resp.text
    assert await _stored(db_session, public_project) == []


# ---------------------------------------------------------------------------
# Making a project private clears the list
# ---------------------------------------------------------------------------


async def test_making_a_project_private_clears_it_and_audits_the_change(
    client: AsyncClient, db_session: AsyncSession, admin: User, manager: User, public_project: Project
) -> None:
    await set_anonymous_permissions(db_session, public_project, ["view_issues", "view_wiki"], admin)
    await db_session.commit()

    resp = await client.patch("/api/v1/projects/APUB/", json={"is_public": False}, headers=_bearer(manager))

    assert resp.status_code == 200, resp.text
    row = (
        await db_session.execute(
            text("SELECT is_public, anonymous_permissions FROM projects WHERE id = :id"), {"id": public_project.id}
        )
    ).one()
    assert row.is_public is False
    assert row.anonymous_permissions == []

    rows = await _audit_rows(db_session, public_project)
    assert rows[-1].user_id == manager.id
    assert rows[-1].details == {
        "project_key": "APUB",
        "old": ["view_issues", "view_wiki"],
        "new": [],
        "reason": "project_made_private",
    }


async def test_making_a_project_private_without_permissions_writes_no_audit_entry(
    client: AsyncClient, db_session: AsyncSession, manager: User, public_project: Project
) -> None:
    resp = await client.patch("/api/v1/projects/APUB/", json={"is_public": False}, headers=_bearer(manager))

    assert resp.status_code == 200, resp.text
    assert await _audit_rows(db_session, public_project) == []


async def test_other_project_updates_leave_it_alone(
    client: AsyncClient, db_session: AsyncSession, admin: User, manager: User, public_project: Project
) -> None:
    await set_anonymous_permissions(db_session, public_project, ["view_wiki"], admin)
    await db_session.commit()

    resp = await client.patch(
        "/api/v1/projects/APUB/", json={"is_public": True, "description": "Still public"}, headers=_bearer(manager)
    )

    assert resp.status_code == 200, resp.text
    assert await _stored(db_session, public_project) == ["view_wiki"]


# ---------------------------------------------------------------------------
# Project settings page
# ---------------------------------------------------------------------------


_MARKER = 'data-testid="anonymous-access-settings"'


async def test_settings_page_shows_the_section_to_an_admin_on_a_public_project(
    client: AsyncClient, admin: User, public_project: Project
) -> None:
    resp = await client.get("/projects/APUB/settings/", cookies=_cookies(admin))

    assert resp.status_code == 200
    assert _MARKER in resp.text
    assert "projectAnonymousAccess(" in resp.text


async def test_settings_page_hides_the_section_from_a_project_manager(
    client: AsyncClient, db_session: AsyncSession, admin: User, manager: User, public_project: Project
) -> None:
    await set_anonymous_permissions(db_session, public_project, ["view_issues"], admin)
    await db_session.commit()

    resp = await client.get("/projects/APUB/settings/", cookies=_cookies(manager))

    assert resp.status_code == 200
    assert _MARKER not in resp.text
    assert "projectAnonymousAccess(" not in resp.text


async def test_settings_page_hides_the_section_on_a_private_project(
    client: AsyncClient, admin: User, private_project: Project
) -> None:
    resp = await client.get("/projects/APRIV/settings/", cookies=_cookies(admin))

    assert resp.status_code == 200
    assert _MARKER not in resp.text
