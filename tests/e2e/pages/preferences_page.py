"""Page Object Model for the user preferences page."""

from __future__ import annotations

from playwright.sync_api import Page, expect


class PreferencesPage:
    def __init__(self, page: Page) -> None:
        self.page = page
        self.color_swatches = page.locator(".sp-color-swatch")

    def navigate(self) -> None:
        self.page.goto("/my/preferences/")

    def expect_loaded(self) -> None:
        expect(self.page.locator("h1")).to_be_visible()
