"""Page Object Model for the search page."""

from __future__ import annotations

from playwright.sync_api import Page, expect


class SearchPage:
    def __init__(self, page: Page) -> None:
        self.page = page
        self.search_input = page.locator(".search-box input[name='q']")
        self.mode_buttons = page.locator(".mode-toggle-btn")
        self.scope_buttons = page.locator(".scope-tab")

    def navigate(self, query: str = "") -> None:
        url = "/search/"
        if query:
            url += f"?q={query}"
        self.page.goto(url)

    def search(self, query: str) -> None:
        self.search_input.fill(query)
        self.search_input.press("Enter")
        self.page.wait_for_load_state("networkidle")

    def expect_loaded(self) -> None:
        expect(self.page.locator("h1", has_text="Search")).to_be_visible()
        expect(self.search_input).to_be_visible()

    def expect_results_count(self, text: str) -> None:
        """Check the results summary, e.g. '3 results for "test"'."""
        expect(self.page.locator(f"text={text}")).to_be_visible()

    def select_mode(self, mode: str) -> None:
        self.page.locator(f".mode-toggle-btn:has-text('{mode.capitalize()}')").click()
        self.page.wait_for_load_state("networkidle")
