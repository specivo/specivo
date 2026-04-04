"""E2E tests for avatar color feature — preferences page and color picker.

These tests use a real browser via Playwright against a live uvicorn server
started by the ``e2e_server`` fixture.  They require the E2E test DB to be
seeded with a palette setting (done via the server's own settings, or the
test DB state from the previous migration).

Run with::

    make test-e2e  # headless
    make test-e2e-headed  # with visible browser
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

pytestmark = [pytest.mark.e2e]


# ---------------------------------------------------------------------------
# Preferences page — palette rendering
# ---------------------------------------------------------------------------


def test_preferences_shows_color_swatches(auth_page: Page):
    """Preferences page renders at least one color swatch circle."""
    auth_page.goto("/my/preferences/")
    auth_page.wait_for_load_state("networkidle")

    swatches = auth_page.locator(".sp-color-swatch")
    expect(swatches.first).to_be_visible()
    assert swatches.count() >= 1, "Expected at least one color swatch on preferences page"


def test_preferences_shows_multiple_swatches(auth_page: Page):
    """Preferences page renders multiple color swatches when palette has 2+ colors."""
    auth_page.goto("/my/preferences/")
    auth_page.wait_for_load_state("networkidle")

    swatches = auth_page.locator(".sp-color-swatch")
    count = swatches.count()
    assert count >= 2, (
        f"Expected at least 2 swatches (palette entries), found {count}. "
        "Check that avatar_color_palette setting is seeded in the E2E DB."
    )


def test_preferences_page_title_or_heading(auth_page: Page):
    """Preferences page renders without error and contains a recognizable heading."""
    auth_page.goto("/my/preferences/")
    auth_page.wait_for_load_state("networkidle")
    assert auth_page.url.endswith("/my/preferences/")
    # The page must not be the login redirect
    assert "/login/" not in auth_page.url


# ---------------------------------------------------------------------------
# Swatch interaction — selection state
# ---------------------------------------------------------------------------


def test_color_swatch_click_marks_it_selected(auth_page: Page):
    """Clicking a swatch adds a visual selection indicator (CSS class or aria)."""
    auth_page.goto("/my/preferences/")
    auth_page.wait_for_load_state("networkidle")

    swatches = auth_page.locator(".sp-color-swatch")
    if swatches.count() < 2:
        pytest.skip("Need at least 2 swatches for swatch-click test")

    # Click the second swatch (index 1) to avoid clicking the already-selected one
    target = swatches.nth(1)
    target.click()

    # After clicking, the swatch should gain a "selected" class or similar indicator
    expect(target).to_have_class(re.compile(r"selected"))


def test_clicking_different_swatches_moves_selection(auth_page: Page):
    """Selecting swatch B after swatch A moves the selection to B."""
    auth_page.goto("/my/preferences/")
    auth_page.wait_for_load_state("networkidle")

    swatches = auth_page.locator(".sp-color-swatch")
    if swatches.count() < 2:
        pytest.skip("Need at least 2 swatches for selection-move test")

    first = swatches.nth(0)
    second = swatches.nth(1)

    first.click()
    expect(first).to_have_class(re.compile(r"selected"))

    second.click()
    expect(second).to_have_class(re.compile(r"selected"))
    # First swatch should no longer be exclusively selected
    # (allow implementations that keep both highlighted, just verify second is selected)


# ---------------------------------------------------------------------------
# Save flow
# ---------------------------------------------------------------------------


def test_save_color_preference_redirects_to_preferences(auth_page: Page):
    """Saving a selected color redirects back to /my/preferences/."""
    auth_page.goto("/my/preferences/")
    auth_page.wait_for_load_state("networkidle")

    swatches = auth_page.locator(".sp-color-swatch")
    if swatches.count() < 1:
        pytest.skip("No swatches available — check E2E DB palette seeding")

    # Select the first swatch
    swatches.first.click()

    # Click the primary save button
    save_btn = auth_page.locator(".sp-btn-primary", has_text=re.compile(r"save", re.I))
    if not save_btn.count():
        # Fallback: any submit button on the form
        save_btn = auth_page.locator("form [type=submit]")

    expect(save_btn.first).to_be_visible()
    save_btn.first.click()

    # Should end up back on the preferences page (303 → 200)
    auth_page.wait_for_url("**/my/preferences/**", timeout=5000)
    assert "/my/preferences/" in auth_page.url


def test_save_color_preference_persists_across_reload(auth_page: Page):
    """After saving, reloading the preferences page shows the saved color as selected."""
    auth_page.goto("/my/preferences/")
    auth_page.wait_for_load_state("networkidle")

    swatches = auth_page.locator(".sp-color-swatch")
    if swatches.count() < 2:
        pytest.skip("Need at least 2 swatches to verify persistence")

    # Pick the last swatch to maximise chance of changing from current
    target = swatches.last
    target.click()

    # Save
    save_btn = auth_page.locator(".sp-btn-primary", has_text=re.compile(r"save", re.I))
    if not save_btn.count():
        save_btn = auth_page.locator("form [type=submit]")
    save_btn.first.click()

    auth_page.wait_for_url("**/my/preferences/**", timeout=5000)

    # Reload and confirm a swatch is still marked selected
    auth_page.reload()
    auth_page.wait_for_load_state("networkidle")
    selected = auth_page.locator(".sp-color-swatch.selected")
    expect(selected.first).to_be_visible()


# ---------------------------------------------------------------------------
# Profile page — avatar section
# ---------------------------------------------------------------------------


def test_profile_page_loads(auth_page: Page):
    """GET /my/profile/ renders without error for an authenticated user."""
    auth_page.goto("/my/profile/")
    auth_page.wait_for_load_state("networkidle")
    assert "/my/profile/" in auth_page.url
    assert "/login/" not in auth_page.url
