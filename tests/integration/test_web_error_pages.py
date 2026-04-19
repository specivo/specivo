"""Web error page integration tests.

Verifies that web routes return styled HTML error pages (not raw JSON)
when a browser requests a resource that triggers an HTTP error.

The exception handler in specivo.core.exceptions renders HTML for
requests with Accept: text/html, and JSON for API clients.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.project import Project
from tests.factories.project import ProjectFactory

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BROWSER_HEADERS = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
API_HEADERS = {"Accept": "application/json"}

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def _project(db_session: AsyncSession) -> Project:
    """Persisted test project for error page tests."""
    proj = ProjectFactory.build(key="ERR", identifier="error-page-test")
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


# ---------------------------------------------------------------------------
# Tests: HTML error pages for browser requests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_wiki_page_not_found_returns_html(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    _project: Project,
):
    """GET /projects/{key}/wiki/nonexistent-slug/ with browser Accept header
    returns 404 with styled HTML, not JSON."""
    token = admin_client.state.token
    resp = await admin_client.get(
        f"/projects/{_project.key}/wiki/nonexistent-slug/",
        cookies={"access_token": token},
        headers=BROWSER_HEADERS,
    )
    assert resp.status_code == 404
    assert "text/html" in resp.headers.get("content-type", "")
    assert "Page not found" in resp.text
    assert '"errors"' not in resp.text, "Should not return JSON error envelope"


@pytest.mark.integration
async def test_wiki_project_not_found_returns_html(
    admin_client: AsyncClient,
    db_session: AsyncSession,
):
    """GET /projects/NONEXISTENT/wiki/ returns 404 styled HTML."""
    token = admin_client.state.token
    resp = await admin_client.get(
        "/projects/NONEXISTENT/wiki/",
        cookies={"access_token": token},
        headers=BROWSER_HEADERS,
    )
    assert resp.status_code == 404
    assert "text/html" in resp.headers.get("content-type", "")
    assert '"errors"' not in resp.text


@pytest.mark.integration
async def test_issue_not_found_returns_html(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    _project: Project,
):
    """GET /issue/FAKE-999/ returns 404 styled HTML."""
    token = admin_client.state.token
    resp = await admin_client.get(
        "/issue/FAKE-999/",
        cookies={"access_token": token},
        headers=BROWSER_HEADERS,
    )
    assert resp.status_code == 404
    assert "text/html" in resp.headers.get("content-type", "")
    assert '"errors"' not in resp.text


@pytest.mark.integration
async def test_project_not_found_returns_html(
    admin_client: AsyncClient,
    db_session: AsyncSession,
):
    """GET /projects/NONEXISTENT/ returns 404 styled HTML."""
    token = admin_client.state.token
    resp = await admin_client.get(
        "/projects/NONEXISTENT/",
        cookies={"access_token": token},
        headers=BROWSER_HEADERS,
    )
    assert resp.status_code == 404
    assert "text/html" in resp.headers.get("content-type", "")
    assert '"errors"' not in resp.text


# ---------------------------------------------------------------------------
# Tests: JSON for API clients
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_404_returns_json_for_api_client(
    admin_client: AsyncClient,
    db_session: AsyncSession,
):
    """Same 404 URL but with Accept: application/json returns JSON, not HTML."""
    token = admin_client.state.token
    resp = await admin_client.get(
        "/projects/NONEXISTENT/",
        cookies={"access_token": token},
        headers=API_HEADERS,
    )
    assert resp.status_code == 404
    body = resp.json()
    assert "errors" in body
    assert body["errors"][0]["code"] == "not_found"


# ---------------------------------------------------------------------------
# Tests: unmatched-route 404 (raised by Starlette's router, not in-app code)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_unmatched_route_returns_html_for_browser(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    _project: Project,
):
    """URL that does not match any registered route still returns styled HTML.

    The router raises starlette.exceptions.HTTPException(404) before reaching
    any view, so this exercises the exception handler's registration against
    the Starlette parent class (not just fastapi.HTTPException).
    """
    token = admin_client.state.token
    resp = await admin_client.get(
        f"/projects/{_project.key}/wiki/parent-slug/child-slug/",
        cookies={"access_token": token},
        headers=BROWSER_HEADERS,
    )
    assert resp.status_code == 404
    assert "text/html" in resp.headers.get("content-type", "")
    assert '"detail"' not in resp.text, "Should not return raw Starlette JSON"
    assert '"errors"' not in resp.text, "Should not return API JSON envelope"


@pytest.mark.integration
async def test_unmatched_route_returns_json_for_api_client(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    _project: Project,
):
    """Same unmatched URL with Accept: application/json returns the app's JSON envelope."""
    token = admin_client.state.token
    resp = await admin_client.get(
        f"/projects/{_project.key}/wiki/parent-slug/child-slug/",
        cookies={"access_token": token},
        headers=API_HEADERS,
    )
    assert resp.status_code == 404
    body = resp.json()
    assert "errors" in body, "Unmatched route should use the app's error envelope, not raw {detail: ...}"
    assert body["errors"][0]["code"] == "not_found"


# ---------------------------------------------------------------------------
# Tests: error page structure
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_error_page_contains_error_card(
    admin_client: AsyncClient,
    db_session: AsyncSession,
):
    """HTML error response contains the styled error-card CSS class."""
    token = admin_client.state.token
    resp = await admin_client.get(
        "/projects/NONEXISTENT/",
        cookies={"access_token": token},
        headers=BROWSER_HEADERS,
    )
    assert resp.status_code == 404
    assert "text/html" in resp.headers.get("content-type", "")
    # The error template uses either error-content or error-card
    assert "error-content" in resp.text or "error-card" in resp.text
