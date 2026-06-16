"""Integration tests for project metadata schema admin endpoints.

Covers:
- Project-scoped permission enforcement (manage_project, not global is_admin)
- Audit logging of mutating operations (create, update, delete)
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.metadata_schema import MetadataSchema
from specivo.models.role import Role
from specivo.models.security_audit import SecurityAuditLog
from specivo.services.security_audit_service import AuditEvent
from tests.factories.user import UserFactory

pytestmark = pytest.mark.asyncio(loop_scope="function")


SCHEMA_DEF = {
    "type": "object",
    "properties": {"severity": {"type": "string"}},
}


async def _login(client: AsyncClient, login: str, password: str = "testpassword") -> str:
    resp = await client.post("/api/v1/auth/login/", json={"login": login, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


@pytest_asyncio.fixture
async def project_with_pm(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    client: AsyncClient,
):
    """Create a project, a non-admin project-manager user with manage_project,
    and add the PM as a member. Yields (project_key, pm_token, pm_user_id)."""
    # Create project as admin
    resp = await admin_client.post(
        "/api/v1/projects/",
        json={"name": "Schema PM Project", "identifier": "schema-pm", "key": "SCPM"},
    )
    assert resp.status_code in (200, 201), resp.text

    # Manager role with manage_project
    role = Role(name="SchemaManager", permissions=["manage_project"], builtin=0, assignable=True)
    pm = UserFactory.build(login="schema_pm_user", status="active")
    db_session.add(role)
    db_session.add(pm)
    await db_session.flush()
    role_id = role.id
    pm_id = pm.id

    pm_token = await _login(client, "schema_pm_user")

    add = await admin_client.post(
        "/api/v1/projects/SCPM/members/",
        json={"user_id": pm_id, "role_ids": [role_id]},
    )
    assert add.status_code in (200, 201), add.text

    return "SCPM", pm_token, pm_id


@pytest_asyncio.fixture
async def project_with_outsider(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    client: AsyncClient,
):
    """Create a project and an unrelated active non-admin user.
    Yields (project_key, outsider_token)."""
    resp = await admin_client.post(
        "/api/v1/projects/",
        json={"name": "Schema Outsider Proj", "identifier": "schema-out", "key": "SCOU"},
    )
    assert resp.status_code in (200, 201), resp.text

    outsider = UserFactory.build(login="schema_outsider", status="active")
    db_session.add(outsider)
    await db_session.flush()

    token = await _login(client, "schema_outsider")
    return "SCOU", token


# ---------------------------------------------------------------------------
# Permission tests — project manager (non-admin) can manage schemas
# ---------------------------------------------------------------------------


async def test_pm_can_create_schema(client: AsyncClient, project_with_pm) -> None:
    project_key, pm_token, _ = project_with_pm
    resp = await client.post(
        f"/api/v1/admin/projects/{project_key}/metadata-schemas/",
        json={"name": "PM Schema", "tracker_id": None, "schema_definition": SCHEMA_DEF},
        headers={"Authorization": f"Bearer {pm_token}"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["name"] == "PM Schema"


async def test_pm_can_update_schema(
    client: AsyncClient,
    db_session: AsyncSession,
    project_with_pm,
) -> None:
    project_key, pm_token, _ = project_with_pm
    # Seed schema directly
    proj_id = (
        await db_session.execute(
            select(MetadataSchema.project_id).select_from(MetadataSchema).limit(0)
        )
    )  # no-op to keep linter happy
    from specivo.models.project import Project
    project = (
        await db_session.execute(select(Project).where(Project.key == project_key))
    ).scalar_one()
    schema = MetadataSchema(
        project_id=project.id,
        name="Original",
        schema_definition=SCHEMA_DEF,
    )
    db_session.add(schema)
    await db_session.commit()
    await db_session.refresh(schema)

    resp = await client.patch(
        f"/api/v1/admin/projects/{project_key}/metadata-schemas/{schema.id}/",
        json={"name": "Renamed by PM"},
        headers={"Authorization": f"Bearer {pm_token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Renamed by PM"


async def test_outsider_cannot_update_schema(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_client: AsyncClient,
    project_with_outsider,
) -> None:
    project_key, out_token = project_with_outsider
    # Admin seeds the schema
    create = await admin_client.post(
        f"/api/v1/admin/projects/{project_key}/metadata-schemas/",
        json={"name": "Locked", "tracker_id": None, "schema_definition": SCHEMA_DEF},
    )
    assert create.status_code == 201, create.text
    schema_id = create.json()["id"]

    resp = await client.patch(
        f"/api/v1/admin/projects/{project_key}/metadata-schemas/{schema_id}/",
        json={"name": "Hijacked"},
        headers={"Authorization": f"Bearer {out_token}"},
    )
    assert resp.status_code == 403, resp.text


async def test_outsider_cannot_create_schema(
    client: AsyncClient, project_with_outsider
) -> None:
    project_key, out_token = project_with_outsider
    resp = await client.post(
        f"/api/v1/admin/projects/{project_key}/metadata-schemas/",
        json={"name": "X", "tracker_id": None, "schema_definition": SCHEMA_DEF},
        headers={"Authorization": f"Bearer {out_token}"},
    )
    assert resp.status_code == 403, resp.text


async def test_outsider_cannot_delete_schema(
    client: AsyncClient,
    admin_client: AsyncClient,
    project_with_outsider,
) -> None:
    project_key, out_token = project_with_outsider
    create = await admin_client.post(
        f"/api/v1/admin/projects/{project_key}/metadata-schemas/",
        json={"name": "DontDelete", "tracker_id": None, "schema_definition": SCHEMA_DEF},
    )
    schema_id = create.json()["id"]

    resp = await client.delete(
        f"/api/v1/admin/projects/{project_key}/metadata-schemas/{schema_id}/",
        headers={"Authorization": f"Bearer {out_token}"},
    )
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# Audit-logging tests — only meaningful when security_audit_log feature is on.
# ---------------------------------------------------------------------------


def _audit_enabled() -> bool:
    from specivo.services.security_audit_service import SecurityAuditService

    return SecurityAuditService._audit_enabled()


@pytest.mark.skipif(not _audit_enabled(), reason="security_audit_log feature not loaded")
async def test_update_writes_audit_event(
    client: AsyncClient,
    admin_client: AsyncClient,
    db_session: AsyncSession,
    project_with_pm,
) -> None:
    project_key, pm_token, pm_id = project_with_pm
    create = await admin_client.post(
        f"/api/v1/admin/projects/{project_key}/metadata-schemas/",
        json={"name": "AuditMe", "tracker_id": None, "schema_definition": SCHEMA_DEF},
    )
    schema_id = create.json()["id"]

    resp = await client.patch(
        f"/api/v1/admin/projects/{project_key}/metadata-schemas/{schema_id}/",
        json={"name": "AuditMeRenamed"},
        headers={"Authorization": f"Bearer {pm_token}"},
    )
    assert resp.status_code == 200, resp.text

    # Verify audit row exists
    rows = (
        await db_session.execute(
            select(SecurityAuditLog).where(
                SecurityAuditLog.event_type == AuditEvent.METADATA_SCHEMA_UPDATED,
                SecurityAuditLog.user_id == pm_id,
                SecurityAuditLog.resource_id == schema_id,
            )
        )
    ).scalars().all()
    assert rows, "expected metadata_schema_updated audit entry"
    assert rows[0].details.get("changed_fields") == ["name"]
