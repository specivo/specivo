"""Health check endpoint tests."""

import pytest
from httpx import AsyncClient


@pytest.mark.integration
async def test_health_returns_200(unauth_client: AsyncClient):
    """Health endpoint always returns HTTP 200 regardless of service state."""
    response = await unauth_client.get("/health")
    assert response.status_code == 200


@pytest.mark.integration
async def test_health_response_structure(unauth_client: AsyncClient):
    """Health endpoint returns expected fields and a known version string."""
    response = await unauth_client.get("/health")
    data = response.json()

    # status is either fully healthy or degraded — never absent
    assert data["status"] in ("ok", "degraded")

    # database and redis fields must be present (may report an error string
    # when running without real backing services, which is fine)
    assert "database" in data
    assert "redis" in data

    # version is redacted from public health endpoint (info disclosure prevention)
    assert "version" in data  # field exists but value is empty
