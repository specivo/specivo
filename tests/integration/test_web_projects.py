"""Web project page integration tests.

Verifies project list, detail, and settings pages render correctly
with proper auth checks and content.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.schemas.project import ProjectCreate
from specivo.services.project_service import ProjectService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_svc = ProjectService()


async def _create_project(
    db_session: AsyncSession,
    user,
    *,
    name: str = "Web Test Project",
    identifier: str = "web-test-proj",
    key: str = "WTP",
    is_public: bool = False,
) -> object:
    """Create a project via the service layer and commit."""
    data = ProjectCreate(name=name, identifier=identifier, key=key, is_public=is_public)
    project = await _svc.create(db_session, data, user)
    await db_session.commit()
    await db_session.refresh(project)
    return project


# ---------------------------------------------------------------------------
# Tests: project list page
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_projects_list_page(auth_client: AsyncClient):
    """GET /projects with auth returns 200 and contains 'Projects'."""
    token = auth_client.state.token
    resp = await auth_client.get(
        "/projects/",
        cookies={"access_token": token},
    )
    assert resp.status_code == 200
    assert "Projects" in resp.text


@pytest.mark.integration
async def test_projects_list_requires_auth(unauth_client: AsyncClient):
    """GET /projects without auth redirects to /login."""
    resp = await unauth_client.get("/projects/", follow_redirects=False)
    assert resp.status_code == 302
    assert "/login/" in resp.headers["location"]


# ---------------------------------------------------------------------------
# Tests: project detail page
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_project_detail_page(
    admin_client: AsyncClient,
    db_session: AsyncSession,
):
    """GET /projects/{key} with auth returns 200 and contains the project name."""
    user = admin_client.state.user
    project = await _create_project(db_session, user, key="WDP", identifier="web-detail-proj")
    token = admin_client.state.token
    resp = await admin_client.get(
        f"/projects/{project.key}/",
        cookies={"access_token": token},
    )
    assert resp.status_code == 200
    assert project.name in resp.text


# ---------------------------------------------------------------------------
# Tests: project settings page
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_project_settings_page(
    admin_client: AsyncClient,
    db_session: AsyncSession,
):
    """GET /projects/{key}/settings with admin returns 200 and contains 'Settings'."""
    user = admin_client.state.user
    project = await _create_project(db_session, user, key="WSP", identifier="web-settings-proj")
    token = admin_client.state.token
    resp = await admin_client.get(
        f"/projects/{project.key}/settings/",
        cookies={"access_token": token},
    )
    assert resp.status_code == 200
    assert "Settings" in resp.text


@pytest.mark.integration
async def test_project_settings_requires_admin(
    auth_client: AsyncClient,
    db_session: AsyncSession,
):
    """GET /projects/{key}/settings as regular user returns 403."""
    user = auth_client.state.user
    project = await _create_project(db_session, user, key="WSR", identifier="web-settings-regular", is_public=True)
    token = auth_client.state.token
    resp = await auth_client.get(
        f"/projects/{project.key}/settings/",
        cookies={"access_token": token},
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Tests: project tree (subprojects in list view)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_projects_list_shows_subproject_nested(
    admin_client: AsyncClient,
    db_session: AsyncSession,
):
    """Subproject appears in the project list page under its parent."""
    user = admin_client.state.user
    parent = await _create_project(db_session, user, name="TreeParent", key="TPAR", identifier="tree-parent")
    child_data = ProjectCreate(name="TreeChild", identifier="tree-child", key="TCHD", parent_key="TPAR")
    child = await _svc.create(db_session, child_data, user)
    await db_session.commit()

    token = admin_client.state.token
    resp = await admin_client.get("/projects/", cookies={"access_token": token})
    assert resp.status_code == 200
    # Both parent and child names should appear
    assert "TreeParent" in resp.text
    assert "TreeChild" in resp.text


@pytest.mark.integration
async def test_projects_list_private_parent_shows_child_as_root(
    auth_client: AsyncClient,
    admin_client: AsyncClient,
    db_session: AsyncSession,
):
    """When user can see a child but not its private parent, child shows as root."""
    admin = admin_client.state.user
    regular_user = auth_client.state.user

    # Create private parent (regular user NOT a member)
    parent_data = ProjectCreate(
        name="PrivParent",
        identifier="priv-parent",
        key="PPAR",
        is_public=False,
    )
    parent = await _svc.create(db_session, parent_data, admin)
    await db_session.commit()

    # Create public child under the private parent
    child_data = ProjectCreate(
        name="PubChild",
        identifier="pub-child",
        key="PCHD",
        parent_key="PPAR",
        is_public=True,
    )
    child = await _svc.create(db_session, child_data, admin)
    await db_session.commit()

    # Regular user can see the public child but not the private parent.
    # The child should appear as a root-level project (not hidden).
    token = auth_client.state.token
    resp = await auth_client.get("/projects/", cookies={"access_token": token})
    assert resp.status_code == 200
    assert "PubChild" in resp.text
    # The private parent should NOT appear
    assert "PrivParent" not in resp.text


@pytest.mark.integration
async def test_project_detail_shows_subprojects(
    admin_client: AsyncClient,
    db_session: AsyncSession,
):
    """Project detail page lists subprojects."""
    user = admin_client.state.user
    parent = await _create_project(db_session, user, name="DetailPar", key="DPAR", identifier="detail-parent")
    child_data = ProjectCreate(name="DetailChild", identifier="detail-child", key="DCHD", parent_key="DPAR")
    await _svc.create(db_session, child_data, user)
    await db_session.commit()

    token = admin_client.state.token
    resp = await admin_client.get(f"/projects/{parent.key}/", cookies={"access_token": token})
    assert resp.status_code == 200
    assert "Subprojects" in resp.text
    assert "DetailChild" in resp.text
