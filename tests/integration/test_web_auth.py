"""Web auth page integration tests.

Verifies login page rendering, logout redirect, and API keys page
access control (auth required) and rendering.
"""

import pytest
from httpx import AsyncClient


@pytest.mark.integration
async def test_login_page_renders(unauth_client: AsyncClient):
    """GET /login returns 200 and contains 'Sign in'."""
    resp = await unauth_client.get("/login")
    assert resp.status_code == 200
    assert "Sign in" in resp.text


@pytest.mark.integration
async def test_login_page_has_form(unauth_client: AsyncClient):
    """Login page contains an Alpine.js loginForm component."""
    resp = await unauth_client.get("/login")
    assert resp.status_code == 200
    assert "x-data" in resp.text
    assert "loginForm" in resp.text


@pytest.mark.integration
async def test_logout_redirects(unauth_client: AsyncClient):
    """GET /logout returns 302 redirect to /login."""
    resp = await unauth_client.get("/logout", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"


@pytest.mark.integration
async def test_api_keys_requires_auth(unauth_client: AsyncClient):
    """GET /my/api-keys without auth redirects to /login."""
    resp = await unauth_client.get("/my/api-keys", follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers["location"]


@pytest.mark.integration
async def test_api_keys_with_auth(auth_client: AsyncClient):
    """GET /my/api-keys with valid auth cookie returns 200 with API Keys content."""
    # The auth_client has a Bearer token in headers. The web page reads
    # cookies, so we need to login via API and use the cookie that's set.
    # auth_client already logged in via POST /api/v1/auth/login which sets
    # httpOnly cookies on the response. However, httpx AsyncClient with
    # ASGITransport doesn't persist cookies from previous responses by default.
    # We use the token directly via cookie instead.
    token = auth_client.state.token
    resp = await auth_client.get(
        "/my/api-keys",
        cookies={"access_token": token},
    )
    assert resp.status_code == 200
    assert "API Keys" in resp.text
