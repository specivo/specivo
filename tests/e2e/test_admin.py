"""E2E tests for admin pages."""

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.pages.admin_page import AdminDashboardPage

pytestmark = [pytest.mark.e2e]


def test_admin_dashboard_renders(admin_page: Page) -> None:
    """Admin dashboard loads with stats cards."""
    admin = AdminDashboardPage(admin_page)
    admin.navigate()
    admin.expect_loaded()
    admin.expect_stat_visible("Total Users")
    admin.expect_stat_visible("Active Projects")


def test_admin_workflows_page(admin_page: Page) -> None:
    """Admin workflows page loads."""
    admin_page.goto("/admin/workflows/")
    expect(admin_page.locator("h1", has_text="Workflow Transitions")).to_be_visible()


def test_admin_settings_page(admin_page: Page) -> None:
    """Admin settings page loads."""
    admin_page.goto("/admin/settings/")
    expect(admin_page.locator("h1", has_text="Settings")).to_be_visible()


def test_admin_denied_for_regular_user(auth_page: Page) -> None:
    """Regular user gets 403 on admin pages."""
    auth_page.goto("/admin/")
    # Admin pages return JSON 403 for non-admin users
    expect(auth_page.locator("text=Admin access required")).to_be_visible(timeout=3000)
