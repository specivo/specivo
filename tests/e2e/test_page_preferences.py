"""E2E tests for the preferences page — layout, color swatches, header components."""

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.e2e_helpers import (
    ConsoleErrorTracker,
    assert_avatar_popup,
    assert_header_search,
)

pytestmark = [pytest.mark.e2e]


def test_no_console_errors(admin_page: Page) -> None:
    """Preferences page loads without JS console errors."""
    tracker = ConsoleErrorTracker().attach(admin_page)
    admin_page.goto("/my/preferences/")
    admin_page.wait_for_load_state("networkidle")
    tracker.assert_no_errors()


def test_preferences_page_renders(admin_page: Page) -> None:
    """Preferences page shows a heading."""
    admin_page.goto("/my/preferences/")
    admin_page.wait_for_load_state("networkidle")
    expect(admin_page.locator("h1")).to_be_visible()


def test_color_swatches_visible(admin_page: Page) -> None:
    """At least 5 color swatches are visible on the preferences page."""
    admin_page.goto("/my/preferences/")
    admin_page.wait_for_load_state("networkidle")
    swatches = admin_page.locator(".sp-color-swatch")
    expect(swatches.first).to_be_visible()
    assert swatches.count() >= 5, (
        f"Expected at least 5 color swatches, found {swatches.count()}"
    )


def test_header_search(admin_page: Page) -> None:
    """Global search field is visible in the header."""
    admin_page.goto("/my/preferences/")
    assert_header_search(admin_page)


def test_avatar_popup(admin_page: Page) -> None:
    """Avatar dropdown shows Profile, Preferences, API Keys, and Sign out."""
    admin_page.goto("/my/preferences/")
    assert_avatar_popup(admin_page)
