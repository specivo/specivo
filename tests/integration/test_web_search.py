"""Web search page integration tests.

Verifies search page renders with query input, mode toggle,
and proper auth checks.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

# ---------------------------------------------------------------------------
# Tests: search page
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_search_page(auth_client: AsyncClient):
    """GET /search with auth returns 200 and contains search UI."""
    token = auth_client.state.token
    resp = await auth_client.get(
        "/search/?q=test",
        cookies={"access_token": token},
    )
    assert resp.status_code == 200
    assert "Search" in resp.text


@pytest.mark.integration
async def test_search_page_empty_query(auth_client: AsyncClient):
    """GET /search with empty query returns 200."""
    token = auth_client.state.token
    resp = await auth_client.get(
        "/search/",
        cookies={"access_token": token},
    )
    assert resp.status_code == 200
    assert "Search" in resp.text


@pytest.mark.integration
async def test_search_requires_auth(unauth_client: AsyncClient):
    """GET /search without auth redirects to /login."""
    resp = await unauth_client.get("/search/", follow_redirects=False)
    assert resp.status_code == 302
    assert "/login/" in resp.headers["location"]
