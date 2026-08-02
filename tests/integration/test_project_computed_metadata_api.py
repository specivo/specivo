"""Integration tests for the project API/UI surface of computed metadata.

The map lives in ``project.settings["computed_metadata"]`` and used to be
write-only: ``ProjectUpdate`` accepted it but ``ProjectOut`` never echoed it
back, so a configured project was indistinguishable from an unconfigured one.

These tests cover the read-back path, creation, permission gating, and the
project settings page field.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.role import Role
from specivo.services.computed_metadata_service import COMPUTED_METADATA_SETTINGS_KEY
from tests.factories.user import AdminUserFactory, UserFactory

# ---------------------------------------------------------------------------
# Helpers and fixtures
# ---------------------------------------------------------------------------


async def _login(client: AsyncClient, login: str, password: str = "testpassword") -> str:
    resp = await client.post("/api/v1/auth/login/", json={"login": login, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def admin_token(db_session: AsyncSession, client: AsyncClient) -> str:
    user = AdminUserFactory.build(login="pcm_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    return await _login(client, "pcm_admin")


async def _create_project(
    client: AsyncClient,
    token: str,
    key: str,
    identifier: str,
    **extra,
) -> AsyncClient:
    body = {"name": f"Project {key}", "identifier": identifier, "key": key}
    body.update(extra)
    return await client.post("/api/v1/projects/", json=body, headers=_auth(token))


async def _add_member_with_permissions(
    client: AsyncClient,
    db_session: AsyncSession,
    admin_token: str,
    project_key: str,
    login: str,
    role_name: str,
    permissions: list[str],
) -> str:
    """Create a non-admin user, grant it *permissions* on *project_key*, return its token."""
    role = Role(name=role_name, permissions=permissions, builtin=0, assignable=True)
    user = UserFactory.build(login=login, status="active")
    db_session.add(role)
    db_session.add(user)
    await db_session.flush()

    token = await _login(client, login)
    resp = await client.post(
        f"/api/v1/projects/{project_key}/members/",
        json={"user_id": user.id, "role_ids": [role.id]},
        headers=_auth(admin_token),
    )
    assert resp.status_code == 201, resp.text
    return token


# ---------------------------------------------------------------------------
# Read-back on GET
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_get_returns_configured_computed_metadata(client: AsyncClient, admin_token: str) -> None:
    """The value written via PATCH is readable back on GET — the core bug."""
    await _create_project(client, admin_token, "RBK", "readback-proj")

    patch = await client.patch(
        "/api/v1/projects/RBK/",
        json={"computed_metadata": {"Area": "Operations"}},
        headers=_auth(admin_token),
    )
    assert patch.status_code == 200, patch.text
    assert patch.json()["computed_metadata"] == {"Area": "Operations"}

    resp = await client.get("/api/v1/projects/RBK/", headers=_auth(admin_token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["computed_metadata"] == {"Area": "Operations"}


@pytest.mark.integration
async def test_configured_and_unconfigured_projects_are_distinguishable(
    client: AsyncClient, admin_token: str
) -> None:
    """An unconfigured project reports {}, not the same payload as a configured one."""
    await _create_project(client, admin_token, "CFG", "configured-proj")
    await _create_project(client, admin_token, "UNC", "unconfigured-proj")
    await client.patch(
        "/api/v1/projects/CFG/",
        json={"computed_metadata": {"Area": "Finance"}},
        headers=_auth(admin_token),
    )

    configured = await client.get("/api/v1/projects/CFG/", headers=_auth(admin_token))
    unconfigured = await client.get("/api/v1/projects/UNC/", headers=_auth(admin_token))

    assert configured.json()["computed_metadata"] == {"Area": "Finance"}
    assert unconfigured.json()["computed_metadata"] == {}


@pytest.mark.integration
async def test_computed_metadata_survives_a_patch_of_other_fields(
    client: AsyncClient, admin_token: str
) -> None:
    """Editing the name must not silently drop the configured map."""
    await _create_project(client, admin_token, "SRV", "survive-proj", computed_metadata={"Area": "Ops"})

    resp = await client.patch(
        "/api/v1/projects/SRV/",
        json={"name": "Renamed"},
        headers=_auth(admin_token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["computed_metadata"] == {"Area": "Ops"}


@pytest.mark.integration
async def test_empty_map_clears_the_configuration(client: AsyncClient, admin_token: str) -> None:
    """Sending {} removes every computed field (how the UI deletes the last row)."""
    await _create_project(client, admin_token, "CLR", "clear-proj", computed_metadata={"Area": "Ops"})

    resp = await client.patch(
        "/api/v1/projects/CLR/",
        json={"computed_metadata": {}},
        headers=_auth(admin_token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["computed_metadata"] == {}

    get_resp = await client.get("/api/v1/projects/CLR/", headers=_auth(admin_token))
    assert get_resp.json()["computed_metadata"] == {}


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_create_accepts_computed_metadata(client: AsyncClient, admin_token: str) -> None:
    """A project can be created configured, with no follow-up PATCH."""
    resp = await _create_project(
        client, admin_token, "CRT", "create-proj", computed_metadata={"Area": "Support"}
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["computed_metadata"] == {"Area": "Support"}

    get_resp = await client.get("/api/v1/projects/CRT/", headers=_auth(admin_token))
    assert get_resp.json()["computed_metadata"] == {"Area": "Support"}


@pytest.mark.integration
async def test_create_without_computed_metadata_reports_empty(client: AsyncClient, admin_token: str) -> None:
    resp = await _create_project(client, admin_token, "PLN", "plain-proj")
    assert resp.status_code == 201, resp.text
    assert resp.json()["computed_metadata"] == {}


@pytest.mark.integration
async def test_created_computed_metadata_lands_in_project_settings(
    client: AsyncClient, db_session: AsyncSession, admin_token: str
) -> None:
    """Creation writes the documented settings key, not some parallel field."""
    from sqlalchemy import select

    from specivo.models.project import Project

    await _create_project(client, admin_token, "STG", "settings-proj", computed_metadata={"Area": "Legal"})

    result = await db_session.execute(select(Project.settings).where(Project.key == "STG"))
    settings = result.scalar_one()
    assert settings[COMPUTED_METADATA_SETTINGS_KEY] == {"Area": "Legal"}


# ---------------------------------------------------------------------------
# Permission gating
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_member_without_manage_permission_sees_null(
    client: AsyncClient, db_session: AsyncSession, admin_token: str
) -> None:
    """A plain member can read the project but not its computed configuration."""
    await _create_project(client, admin_token, "GTV", "gate-viewer", computed_metadata={"Area": "Ops"})
    viewer_token = await _add_member_with_permissions(
        client, db_session, admin_token, "GTV", "pcm_viewer", "PcmViewer", ["view_issues"]
    )

    resp = await client.get("/api/v1/projects/GTV/", headers=_auth(viewer_token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["computed_metadata"] is None


@pytest.mark.integration
async def test_member_with_manage_permission_sees_the_map(
    client: AsyncClient, db_session: AsyncSession, admin_token: str
) -> None:
    """manage_project is enough — global admin is not required."""
    await _create_project(client, admin_token, "GTM", "gate-manager", computed_metadata={"Area": "Ops"})
    manager_token = await _add_member_with_permissions(
        client, db_session, admin_token, "GTM", "pcm_manager", "PcmManager", ["manage_project"]
    )

    resp = await client.get("/api/v1/projects/GTM/", headers=_auth(manager_token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["computed_metadata"] == {"Area": "Ops"}


@pytest.mark.integration
async def test_non_member_on_public_project_sees_null(
    client: AsyncClient, db_session: AsyncSession, admin_token: str
) -> None:
    """Public visibility grants project access, not configuration access."""
    await _create_project(
        client, admin_token, "PUB", "public-proj", is_public=True, computed_metadata={"Area": "Ops"}
    )
    outsider = UserFactory.build(login="pcm_outsider", status="active")
    db_session.add(outsider)
    await db_session.flush()
    outsider_token = await _login(client, "pcm_outsider")

    resp = await client.get("/api/v1/projects/PUB/", headers=_auth(outsider_token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["computed_metadata"] is None


@pytest.mark.integration
async def test_list_endpoint_omits_computed_metadata(client: AsyncClient, admin_token: str) -> None:
    """The list response never discloses the map — resolving it per row would
    cost one role lookup per project. Documented on ``ProjectOut``."""
    await _create_project(client, admin_token, "LST", "list-proj", computed_metadata={"Area": "Ops"})

    resp = await client.get("/api/v1/projects/", headers=_auth(admin_token))
    assert resp.status_code == 200, resp.text
    listed = [p for p in resp.json()["items"] if p["key"] == "LST"]
    assert listed, resp.text
    assert listed[0]["computed_metadata"] is None


# ---------------------------------------------------------------------------
# Settings page
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_settings_page_renders_computed_field(
    client: AsyncClient, db_session: AsyncSession, admin_token: str
) -> None:
    """The settings page ships the editor seeded with the configured map."""
    await _create_project(client, admin_token, "WEB", "web-proj", computed_metadata={"Area": "Operations"})

    resp = await client.get("/projects/WEB/settings/", cookies={"access_token": admin_token})
    assert resp.status_code == 200, resp.text
    assert "projectComputedMetadata" in resp.text
    assert "Computed Fields" in resp.text
    assert "Operations" in resp.text


@pytest.mark.integration
async def test_settings_page_renders_editor_when_unconfigured(
    client: AsyncClient, db_session: AsyncSession, admin_token: str
) -> None:
    """An unconfigured project still gets the editor, seeded empty."""
    await _create_project(client, admin_token, "WEU", "web-unconfigured")

    resp = await client.get("/projects/WEU/settings/", cookies={"access_token": admin_token})
    assert resp.status_code == 200, resp.text
    assert "projectComputedMetadata({ computedMetadata: {}" in resp.text
