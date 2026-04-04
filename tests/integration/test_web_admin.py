"""Web admin page integration tests.

Verifies admin dashboard, workflows, settings, agent groups,
and kill switch pages render correctly with proper auth checks.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

# ---------------------------------------------------------------------------
# Tests: admin dashboard
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_admin_dashboard(admin_client: AsyncClient):
    """GET /admin with admin returns 200 and contains 'Admin'."""
    token = admin_client.state.token
    resp = await admin_client.get(
        "/admin/",
        cookies={"access_token": token},
    )
    assert resp.status_code == 200
    assert "Admin" in resp.text


@pytest.mark.integration
async def test_admin_requires_admin(auth_client: AsyncClient):
    """GET /admin as regular user returns 403."""
    token = auth_client.state.token
    resp = await auth_client.get(
        "/admin/",
        cookies={"access_token": token},
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Tests: admin sub-pages
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_admin_workflows_page(admin_client: AsyncClient):
    """GET /admin/workflows with admin returns 200."""
    token = admin_client.state.token
    resp = await admin_client.get(
        "/admin/workflows/",
        cookies={"access_token": token},
    )
    assert resp.status_code == 200
    assert "Workflow" in resp.text


@pytest.mark.integration
async def test_admin_settings_page(admin_client: AsyncClient):
    """GET /admin/settings with admin returns 200."""
    token = admin_client.state.token
    resp = await admin_client.get(
        "/admin/settings/",
        cookies={"access_token": token},
    )
    assert resp.status_code == 200
    assert "Settings" in resp.text


@pytest.mark.integration
async def test_admin_agent_groups_page(admin_client: AsyncClient):
    """GET /admin/agent-groups with admin returns 200."""
    token = admin_client.state.token
    resp = await admin_client.get(
        "/admin/agent-groups/",
        cookies={"access_token": token},
    )
    assert resp.status_code == 200
    assert "Agent" in resp.text


@pytest.mark.integration
async def test_admin_kill_switch_page(admin_client: AsyncClient):
    """GET /admin/kill-switch with admin returns 200."""
    token = admin_client.state.token
    resp = await admin_client.get(
        "/admin/kill-switch/",
        cookies={"access_token": token},
    )
    assert resp.status_code == 200
    assert "Kill" in resp.text
