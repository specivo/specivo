"""E2E tests for the dashboard page — layout, sidebar, header components."""

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.e2e_helpers import (
    ConsoleErrorTracker,
    assert_avatar_popup,
    assert_global_sidebar,
    assert_header_search,
)

pytestmark = [pytest.mark.e2e]


def test_no_console_errors(admin_page: Page) -> None:
    """Dashboard loads without JS console errors."""
    tracker = ConsoleErrorTracker().attach(admin_page)
    admin_page.goto("/")
    admin_page.wait_for_load_state("networkidle")
    tracker.assert_no_errors()


def test_dashboard_renders(admin_page: Page) -> None:
    """Dashboard page loads and title contains 'Dashboard'."""
    admin_page.goto("/")
    expect(admin_page.locator("h1, .page-header, .status-banner").first).to_be_visible()


def test_global_sidebar_items(admin_page: Page) -> None:
    """Global sidebar shows Search, Dashboard, Projects, and Admin links."""
    admin_page.goto("/")
    assert_global_sidebar(admin_page, is_admin=True)


def test_admin_link_visible_for_admin(admin_page: Page) -> None:
    """Admin users see the Admin link in the sidebar."""
    admin_page.goto("/")
    admin_link = admin_page.locator("nav.sidebar a[href='/admin/']")
    expect(admin_link).to_be_visible()


def test_admin_link_hidden_for_regular_user(auth_page: Page) -> None:
    """Regular users do NOT see the Admin link in the sidebar."""
    auth_page.goto("/")
    admin_link = auth_page.locator("nav.sidebar a.sidebar-item", has_text="Admin")
    expect(admin_link).not_to_be_visible()


def test_header_search_field(admin_page: Page) -> None:
    """Global search field is visible in the header."""
    admin_page.goto("/")
    assert_header_search(admin_page)


def test_avatar_popup_links(admin_page: Page) -> None:
    """Avatar dropdown shows Profile, Preferences, API Keys, and Sign out."""
    admin_page.goto("/")
    assert_avatar_popup(admin_page)


def test_brand_name_in_sidebar(admin_page: Page) -> None:
    """Sidebar brand shows 'Specivo'."""
    admin_page.goto("/")
    expect(admin_page.locator(".sidebar-brand")).to_contain_text("Specivo")
