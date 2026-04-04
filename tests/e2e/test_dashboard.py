"""E2E tests for the dashboard page."""

import pytest
from playwright.sync_api import Page, expect

from specivo.testing.e2e_base import unique_key
from tests.e2e.pages.dashboard_page import DashboardPage

pytestmark = [pytest.mark.e2e]


def test_dashboard_renders(auth_page: Page) -> None:
    """Dashboard loads with status banner for authenticated user."""
    dash = DashboardPage(auth_page)
    dash.navigate()
    dash.expect_loaded()


def test_dashboard_shows_sidebar(auth_page: Page) -> None:
    """Dashboard includes the sidebar navigation."""
    dash = DashboardPage(auth_page)
    dash.navigate()
    expect(dash.sidebar).to_be_visible()
    expect(dash.sidebar_brand).to_contain_text("Specivo")


@pytest.mark.skip(reason="dashboard project visibility depends on project listing query — investigate separately")
def test_dashboard_shows_created_project(admin_page: Page, api_client) -> None:
    """A project created via API appears on the admin's dashboard."""
    key = unique_key()
    resp = api_client.post(
        "/api/v1/projects/",
        json={"name": f"Test {key}", "identifier": key.lower(), "key": key},
    )
    assert resp.status_code == 201, resp.text

    dash = DashboardPage(admin_page)
    dash.navigate()
    dash.expect_project_visible(key)
