"""E2E tests for the unified search page."""

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.pages.search_page import SearchPage

pytestmark = [pytest.mark.e2e]


def test_search_page_renders(auth_page: Page) -> None:
    """Search page loads with input and mode toggles."""
    search = SearchPage(auth_page)
    search.navigate()
    search.expect_loaded()
    expect(search.mode_buttons.first).to_be_visible()


def test_search_with_empty_query(auth_page: Page) -> None:
    """Visiting search page without query shows no results section."""
    search = SearchPage(auth_page)
    search.navigate()
    # No results summary should be shown when no query
    expect(auth_page.locator("text=results for")).not_to_be_visible()


def test_search_mode_buttons_visible(auth_page: Page) -> None:
    """Search mode buttons (Hybrid, Keyword) are visible."""
    search = SearchPage(auth_page)
    search.navigate()
    expect(auth_page.locator(".mode-toggle-btn:has-text('Keyword')")).to_be_visible()
    expect(auth_page.locator(".mode-toggle-btn:has-text('Hybrid')")).to_be_visible()


def test_search_scope_tabs_visible(auth_page: Page) -> None:
    """All three scope tabs are visible."""
    search = SearchPage(auth_page)
    search.navigate()
    expect(auth_page.locator(".scope-tab:has-text('All')")).to_be_visible()
    expect(auth_page.locator(".scope-tab:has-text('Issues')")).to_be_visible()
    expect(auth_page.locator(".scope-tab:has-text('Wiki')")).to_be_visible()
