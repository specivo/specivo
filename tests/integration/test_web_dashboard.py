"""Web dashboard and notifications page integration tests.

Verifies dashboard renders with auth, redirects without auth,
and notifications page renders correctly.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.integration
async def test_dashboard_renders(auth_client: AsyncClient):
    """GET / with auth returns 200 and contains 'Dashboard'."""
    token = auth_client.state.token
    resp = await auth_client.get(
        "/",
        cookies={"access_token": token},
    )
    assert resp.status_code == 200
    assert "Dashboard" in resp.text


@pytest.mark.integration
async def test_dashboard_requires_auth(unauth_client: AsyncClient):
    """GET / without auth redirects to /login."""
    resp = await unauth_client.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert "/login/" in resp.headers["location"]


# ---------------------------------------------------------------------------
# Tests: notifications page
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_notifications_page(auth_client: AsyncClient):
    """GET /my/notifications with auth returns 200 and contains 'Notifications'."""
    token = auth_client.state.token
    resp = await auth_client.get(
        "/my/notifications/",
        cookies={"access_token": token},
    )
    assert resp.status_code == 200
    assert "Notifications" in resp.text


@pytest.mark.integration
async def test_notifications_requires_auth(unauth_client: AsyncClient):
    """GET /my/notifications without auth redirects to /login."""
    resp = await unauth_client.get("/my/notifications/", follow_redirects=False)
    assert resp.status_code == 302
    assert "/login/" in resp.headers["location"]
