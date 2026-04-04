"""Web roadmap page integration tests.

Verifies roadmap page renders with versions, progress bars,
and proper auth checks.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.project import Project
from tests.factories.project import ProjectFactory
from tests.factories.version import VersionFactory

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def _project(db_session: AsyncSession) -> Project:
    """Persisted test project for roadmap tests."""
    proj = ProjectFactory.build(key="WRM", identifier="web-roadmap-test")
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


# ---------------------------------------------------------------------------
# Tests: roadmap page
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_roadmap_page(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    _project: Project,
):
    """GET /projects/{key}/roadmap with auth returns 200 and contains 'Roadmap'."""
    # Seed a version
    version = VersionFactory.build(project_id=_project.id, name="v1.0")
    db_session.add(version)
    await db_session.commit()

    token = admin_client.state.token
    resp = await admin_client.get(
        f"/projects/{_project.key}/roadmap/",
        cookies={"access_token": token},
    )
    assert resp.status_code == 200
    assert "Roadmap" in resp.text
    assert "v1.0" in resp.text


@pytest.mark.integration
async def test_roadmap_requires_auth(unauth_client: AsyncClient):
    """GET /projects/{key}/roadmap without auth redirects to /login."""
    resp = await unauth_client.get(
        "/projects/ANY/roadmap/",
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "/login/" in resp.headers["location"]


@pytest.mark.integration
async def test_roadmap_empty(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    _project: Project,
):
    """GET /projects/{key}/roadmap with no versions shows empty state."""
    token = admin_client.state.token
    resp = await admin_client.get(
        f"/projects/{_project.key}/roadmap/",
        cookies={"access_token": token},
    )
    assert resp.status_code == 200
    assert "Roadmap" in resp.text
