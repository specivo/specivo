"""Web wiki page integration tests.

Verifies wiki index, show, edit, and history pages render correctly
with proper auth checks and content.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.project import EnabledModule, Project
from specivo.services.wiki_service import WikiService
from tests.factories.project import ProjectFactory

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_wiki_svc = WikiService()


@pytest_asyncio.fixture
async def _wiki_project(db_session: AsyncSession) -> Project:
    """Persisted test project with wiki module enabled."""
    proj = ProjectFactory.build(key="WWK", identifier="web-wiki-test")
    db_session.add(proj)
    await db_session.flush()

    module = EnabledModule(project_id=proj.id, name="wiki")
    db_session.add(module)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


# ---------------------------------------------------------------------------
# Tests: wiki index page
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_wiki_index(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    _wiki_project: Project,
):
    """GET /projects/{key}/wiki returns 200 and contains 'Wiki'."""
    token = admin_client.state.token
    resp = await admin_client.get(
        f"/projects/{_wiki_project.key}/wiki",
        cookies={"access_token": token},
    )
    assert resp.status_code == 200
    assert "Wiki" in resp.text


# ---------------------------------------------------------------------------
# Tests: wiki show page
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_wiki_show_page(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    _wiki_project: Project,
):
    """Create a wiki page, GET /projects/{key}/wiki/{slug} returns 200."""
    user = admin_client.state.user
    page, _content = await _wiki_svc.create_page(db_session, _wiki_project.id, "Test Page", "Hello world", user)
    await db_session.commit()

    token = admin_client.state.token
    resp = await admin_client.get(
        f"/projects/{_wiki_project.key}/wiki/{page.slug}",
        cookies={"access_token": token},
    )
    assert resp.status_code == 200
    assert "Test Page" in resp.text
    assert "Hello world" in resp.text


# ---------------------------------------------------------------------------
# Tests: wiki edit page
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_wiki_edit_page(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    _wiki_project: Project,
):
    """GET /projects/{key}/wiki/{slug}/edit returns 200 with edit form."""
    user = admin_client.state.user
    page, _content = await _wiki_svc.create_page(db_session, _wiki_project.id, "Edit Test", "Initial content", user)
    await db_session.commit()

    token = admin_client.state.token
    resp = await admin_client.get(
        f"/projects/{_wiki_project.key}/wiki/{page.slug}/edit",
        cookies={"access_token": token},
    )
    assert resp.status_code == 200
    assert "Edit Test" in resp.text


# ---------------------------------------------------------------------------
# Tests: wiki history page
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_wiki_history(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    _wiki_project: Project,
):
    """GET /projects/{key}/wiki/{slug}/history returns 200 with version info."""
    user = admin_client.state.user
    page, _content = await _wiki_svc.create_page(db_session, _wiki_project.id, "History Test", "Version one", user)
    await db_session.commit()

    token = admin_client.state.token
    resp = await admin_client.get(
        f"/projects/{_wiki_project.key}/wiki/{page.slug}/history",
        cookies={"access_token": token},
    )
    assert resp.status_code == 200
    assert "History" in resp.text


# ---------------------------------------------------------------------------
# Tests: auth required
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_wiki_requires_auth(unauth_client: AsyncClient):
    """GET /projects/{key}/wiki without auth redirects to /login."""
    resp = await unauth_client.get(
        "/projects/ANY/wiki",
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "/login" in resp.headers["location"]
