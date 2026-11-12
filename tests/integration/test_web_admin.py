"""Web admin page integration tests.

Verifies admin dashboard, workflows and settings pages render correctly with
proper auth checks, and that the unreleased agent groups and kill switch pages
are gone.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

# ---------------------------------------------------------------------------
# Tests: admin pages
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize(
    ("url", "text"),
    [
        pytest.param("/admin/", "Admin", id="dashboard"),
        pytest.param("/admin/workflows/", "Workflow", id="workflows"),
        pytest.param("/admin/settings/", "Settings", id="settings"),
    ],
)
async def test_admin_page_renders(admin_client: AsyncClient, url: str, text: str):
    """An admin page answers 200 and carries its heading."""
    resp = await admin_client.get(url, cookies={"access_token": admin_client.state.token})
    assert resp.status_code == 200
    assert text in resp.text


@pytest.mark.integration
async def test_admin_requires_admin(auth_client: AsyncClient):
    """GET /admin as regular user returns 403."""
    token = auth_client.state.token
    resp = await auth_client.get(
        "/admin/",
        cookies={"access_token": token},
    )
    assert resp.status_code == 403


@pytest.mark.integration
@pytest.mark.parametrize("url", ["/admin/kill-switch/", "/admin/agent-groups/"])
async def test_unreleased_admin_page_is_gone(admin_client: AsyncClient, url: str):
    """The page answers 404, like any unknown admin page."""
    resp = await admin_client.get(url, cookies={"access_token": admin_client.state.token})
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
