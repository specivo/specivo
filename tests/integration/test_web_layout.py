"""Web layout integration tests.

Verifies static file serving, template infrastructure, and regression
checks for existing API/health endpoints after adding the web layer.
"""

import pytest
from httpx import AsyncClient


@pytest.mark.integration
async def test_static_css_served(unauth_client: AsyncClient):
    """Main stylesheet is served from /static/css/specivo.css."""
    resp = await unauth_client.get("/static/css/specivo.css")
    assert resp.status_code == 200
    assert "text/css" in resp.headers["content-type"]


@pytest.mark.integration
async def test_static_css_variables_served(unauth_client: AsyncClient):
    """Design tokens are included in the main specivo.css stylesheet."""
    resp = await unauth_client.get("/static/css/specivo.css")
    assert resp.status_code == 200
    assert "text/css" in resp.headers["content-type"]
    # Should contain our design tokens (merged from variables.css in FE-2)
    assert "--sp-accent" in resp.text


@pytest.mark.integration
async def test_static_js_alpine_served(unauth_client: AsyncClient):
    """Alpine.js vendor file is served from /static/vendor/."""
    resp = await unauth_client.get("/static/vendor/alpine.3.14.min.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]


@pytest.mark.integration
async def test_static_js_htmx_served(unauth_client: AsyncClient):
    """htmx vendor file is served from /static/vendor/."""
    resp = await unauth_client.get("/static/vendor/htmx.2.0.min.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]


@pytest.mark.integration
async def test_static_js_specivo_served(unauth_client: AsyncClient):
    """Custom specivo.js is served."""
    resp = await unauth_client.get("/static/js/specivo.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]


@pytest.mark.integration
async def test_health_still_works(unauth_client: AsyncClient):
    """Regression: health check still returns 200 after web layer added."""
    resp = await unauth_client.get("/health/")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] in ("ok", "degraded")


@pytest.mark.integration
async def test_api_still_works(auth_client: AsyncClient):
    """Regression: API endpoints still work after web layer added."""
    resp = await auth_client.get("/api/v1/projects/")
    assert resp.status_code == 200
