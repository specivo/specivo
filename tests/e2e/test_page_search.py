"""E2E tests for the search page — layout, mode buttons, scope tabs."""

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.e2e_helpers import (
    ConsoleErrorTracker,
    assert_header_search,
)

pytestmark = [pytest.mark.e2e]


def test_no_console_errors(admin_page: Page) -> None:
    """Search page loads without JS console errors."""
    tracker = ConsoleErrorTracker().attach(admin_page)
    admin_page.goto("/search/")
    admin_page.wait_for_load_state("networkidle")
    tracker.assert_no_errors()


def test_search_page_renders(admin_page: Page) -> None:
    """Search page shows the search input."""
    admin_page.goto("/search/")
    admin_page.wait_for_load_state("networkidle")
    expect(admin_page.locator("main input[name='q']")).to_be_visible()


def test_mode_buttons_visible(admin_page: Page) -> None:
    """Hybrid and Keyword mode buttons are visible."""
    admin_page.goto("/search/")
    admin_page.wait_for_load_state("networkidle")
    expect(admin_page.locator(".mode-toggle-btn:has-text('Hybrid')")).to_be_visible()
    expect(admin_page.locator(".mode-toggle-btn:has-text('Keyword')")).to_be_visible()


def test_scope_tabs_visible(admin_page: Page) -> None:
    """All, Issues, Wiki scope tabs are visible."""
    admin_page.goto("/search/")
    admin_page.wait_for_load_state("networkidle")
    expect(admin_page.locator(".scope-tab:has-text('All')")).to_be_visible()
    expect(admin_page.locator(".scope-tab:has-text('Issues')")).to_be_visible()
    expect(admin_page.locator(".scope-tab:has-text('Wiki')")).to_be_visible()


def test_header_search(admin_page: Page) -> None:
    """Global search field is visible in the header."""
    admin_page.goto("/search/")
    assert_header_search(admin_page)
