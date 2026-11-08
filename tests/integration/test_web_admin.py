"""Web admin page integration tests.

Verifies admin dashboard, workflows and settings pages render correctly with
proper auth checks, and that the unreleased agent groups and kill switch pages
are gone.
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
async def test_kill_switch_page_is_gone(admin_client: AsyncClient):
    """GET /admin/kill-switch/ answers 404, like any unknown admin page."""
    resp = await admin_client.get("/admin/kill-switch/", cookies={"access_token": admin_client.state.token})
    assert resp.status_code == 404


@pytest.mark.integration
async def test_agent_groups_page_is_gone(admin_client: AsyncClient):
    """GET /admin/agent-groups/ answers 404, like any unknown admin page."""
    resp = await admin_client.get("/admin/agent-groups/", cookies={"access_token": admin_client.state.token})
    assert resp.status_code == 404


@pytest.mark.integration
async def test_admin_dashboard_has_no_unreleased_sections(admin_client: AsyncClient):
    """The dashboard shows no agent sessions, kill switch or agent group cards."""
    resp = await admin_client.get("/admin/", cookies={"access_token": admin_client.state.token})
    assert resp.status_code == 200
    for gone in (
        "Agent Sessions",
        "Kill Events",
        "Kill Switch",
        "Agent Groups",
        "/admin/kill-switch/",
        "/admin/agent-groups/",
    ):
        assert gone not in resp.text
    assert "Total Users" in resp.text and "Active Projects" in resp.text


@pytest.mark.integration
async def test_sidebar_has_no_security_section(admin_client: AsyncClient):
    """The sidebar has no placeholder agent policy or credential links."""
    resp = await admin_client.get("/", cookies={"access_token": admin_client.state.token})
    assert resp.status_code == 200
    assert "Agent Policies" not in resp.text
    assert 'sidebar-section-label">Security<' not in resp.text
