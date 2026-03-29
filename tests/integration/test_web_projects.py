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
) -> object:
    """Create a project via the service layer and commit."""
    data = ProjectCreate(name=name, identifier=identifier, key=key)
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
        "/projects",
        cookies={"access_token": token},
    )
    assert resp.status_code == 200
    assert "Projects" in resp.text


@pytest.mark.integration
async def test_projects_list_requires_auth(unauth_client: AsyncClient):
    """GET /projects without auth redirects to /login."""
    resp = await unauth_client.get("/projects", follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers["location"]


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
        f"/projects/{project.key}",
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
        f"/projects/{project.key}/settings",
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
    project = await _create_project(db_session, user, key="WSR", identifier="web-settings-regular")
    token = auth_client.state.token
    resp = await auth_client.get(
        f"/projects/{project.key}/settings",
        cookies={"access_token": token},
    )
    assert resp.status_code == 403
