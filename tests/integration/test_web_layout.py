"""Web layout integration tests.

Verifies static file serving, template infrastructure, and regression
checks for existing API/health endpoints after adding the web layer.
"""

import json
import os

import pytest
from httpx import AsyncClient

from specivo.web.assets import DIST_DIR, MANIFEST_SUBDIRS, missing_asset_manifests

# The esbuild bundles are generated, never committed. Without a local build the
# bundle tests skip; CI sets SPECIVO_REQUIRE_BUNDLES=1 so a missing build fails there.
requires_bundles = pytest.mark.skipif(
    bool(missing_asset_manifests()) and not os.environ.get("SPECIVO_REQUIRE_BUNDLES"),
    reason="frontend bundles not built; run `make frontend-build`",
)


@pytest.mark.integration
@requires_bundles
async def test_static_css_served(unauth_client: AsyncClient):
    """Main stylesheet bundle is served from /static/dist/css/."""
    resp = await unauth_client.get("/static/dist/css/specivo.min.css")
    assert resp.status_code == 200
    assert "text/css" in resp.headers["content-type"]


@pytest.mark.integration
@requires_bundles
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
@requires_bundles
@pytest.mark.parametrize("bundle", ["alpine-init.min.js", "app.min.js"])
async def test_static_js_bundles_served(unauth_client: AsyncClient, bundle: str):
    """Custom JS bundles are served from /static/dist/js/."""
    resp = await unauth_client.get(f"/static/dist/js/{bundle}")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]


@pytest.mark.integration
@requires_bundles
def test_asset_manifests_resolve():
    """Every esbuild manifest entry points at a file that exists on disk."""
    assert missing_asset_manifests() == [], "esbuild manifests missing — run `make frontend-build`"
    for sub in MANIFEST_SUBDIRS:
        manifest = DIST_DIR / sub / "manifest.json"
        for logical, served in json.loads(manifest.read_text()).items():
            assert (DIST_DIR / sub / served).exists(), f"{logical} -> {served} missing"


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
