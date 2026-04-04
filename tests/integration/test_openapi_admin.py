"""OpenAPI schema and docs UI endpoints are restricted to admin users.

Non-admin and unauthenticated requests receive 404 (not 401/403)
to avoid revealing that the endpoints exist.
"""

import pytest
from httpx import AsyncClient

OPENAPI_URL = "/api/v1/openapi.json"
DOCS_URL = "/docs"
REDOC_URL = "/redoc"


# ---------------------------------------------------------------------------
# Unauthenticated — must get 404
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_openapi_unauthenticated_returns_404(client: AsyncClient):
    resp = await client.get(OPENAPI_URL)
    assert resp.status_code == 404


@pytest.mark.integration
async def test_docs_unauthenticated_returns_404(client: AsyncClient):
    resp = await client.get(DOCS_URL)
    assert resp.status_code == 404


@pytest.mark.integration
async def test_redoc_unauthenticated_returns_404(client: AsyncClient):
    resp = await client.get(REDOC_URL)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Non-admin authenticated — must get 404
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_openapi_non_admin_returns_404(auth_client: AsyncClient):
    resp = await auth_client.get(OPENAPI_URL)
    assert resp.status_code == 404


@pytest.mark.integration
async def test_docs_non_admin_returns_404(auth_client: AsyncClient):
    resp = await auth_client.get(DOCS_URL)
    assert resp.status_code == 404


@pytest.mark.integration
async def test_redoc_non_admin_returns_404(auth_client: AsyncClient):
    resp = await auth_client.get(REDOC_URL)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Admin — must get 200 with correct content
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_openapi_admin_returns_200_with_schema(admin_client: AsyncClient):
    resp = await admin_client.get(OPENAPI_URL)
    assert resp.status_code == 200
    data = resp.json()
    assert "openapi" in data
    assert "paths" in data
    assert "info" in data


@pytest.mark.integration
async def test_docs_admin_returns_200_html(admin_client: AsyncClient):
    resp = await admin_client.get(DOCS_URL)
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "swagger" in resp.text.lower()


@pytest.mark.integration
async def test_redoc_admin_returns_200_html(admin_client: AsyncClient):
    resp = await admin_client.get(REDOC_URL)
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "redoc" in resp.text.lower()
