"""E2E tests for admin pages — dashboard, access control."""

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.e2e_helpers import (
    ConsoleErrorTracker,
    assert_header_search,
)

pytestmark = [pytest.mark.e2e]


def test_no_console_errors(admin_page: Page) -> None:
    """Admin dashboard loads without JS console errors."""
    tracker = ConsoleErrorTracker().attach(admin_page)
    admin_page.goto("/admin/")
    admin_page.wait_for_load_state("networkidle")
    tracker.assert_no_errors()


def test_admin_dashboard_renders(admin_page: Page) -> None:
    """Admin dashboard loads and shows stat cards."""
    admin_page.goto("/admin/")
    admin_page.wait_for_load_state("networkidle")
    expect(admin_page.locator(".stat-card").first).to_be_visible()


def test_admin_denied_for_regular_user(auth_page: Page) -> None:
    """Regular user gets denied access on admin pages."""
    auth_page.goto("/admin/")
    expect(auth_page.locator("text=Admin access required")).to_be_visible(timeout=3000)


def test_header_search(admin_page: Page) -> None:
    """Global search field is visible in the header."""
    admin_page.goto("/admin/")
    assert_header_search(admin_page)
