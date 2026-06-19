"""Web layout integration tests.

Verifies static file serving, template infrastructure, and regression
checks for existing API/health endpoints after adding the web layer.
"""

import json
from pathlib import Path

import pytest
from httpx import AsyncClient

_DIST = Path(__file__).resolve().parents[2] / "specivo" / "static" / "dist"


@pytest.mark.integration
async def test_static_css_served(unauth_client: AsyncClient):
    """Main stylesheet bundle is served from /static/dist/css/."""
    resp = await unauth_client.get("/static/dist/css/specivo.min.css")
    assert resp.status_code == 200
    assert "text/css" in resp.headers["content-type"]


@pytest.mark.integration
async def test_static_css_variables_served(unauth_client: AsyncClient):
    """Design tokens are included in the main stylesheet bundle."""
    resp = await unauth_client.get("/static/dist/css/specivo.min.css")
    assert resp.status_code == 200
    assert "text/css" in resp.headers["content-type"]
    assert "--sp-accent" in resp.text


@pytest.mark.integration
async def test_static_js_alpine_served(unauth_client: AsyncClient):
    """Alpine.js vendor file is served from /static/vendor/."""
    resp = await unauth_client.get("/static/vendor/alpine.csp.3.14.min.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]


@pytest.mark.integration
async def test_static_js_htmx_served(unauth_client: AsyncClient):
    """htmx vendor file is served from /static/vendor/."""
    resp = await unauth_client.get("/static/vendor/htmx.2.0.min.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]


@pytest.mark.integration
@pytest.mark.parametrize("bundle", ["alpine-init.min.js", "app.min.js"])
async def test_static_js_bundles_served(unauth_client: AsyncClient, bundle: str):
    """Custom JS bundles are served from /static/dist/js/."""
    resp = await unauth_client.get(f"/static/dist/js/{bundle}")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]


@pytest.mark.integration
def test_asset_manifests_resolve():
    """Every esbuild manifest entry points at a file that exists on disk."""
    found = False
    for sub in ("js", "css"):
        manifest = _DIST / sub / "manifest.json"
        if not manifest.exists():
            continue
        found = True
        for logical, hashed in json.loads(manifest.read_text()).items():
            assert (_DIST / sub / hashed).exists(), f"{logical} -> {hashed} missing"
    assert found, "no esbuild manifests found — run `npm run build` in frontend/"


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
