"""Page Object Model for the project overview page."""

from __future__ import annotations

from playwright.sync_api import Page, expect


class ProjectPage:
    def __init__(self, page: Page) -> None:
        self.page = page
        self.heading = page.locator("h1").first
        self.sidebar = page.locator("nav.sidebar")

    def navigate(self, project_key: str) -> None:
        self.page.goto(f"/projects/{project_key}/")

    def expect_loaded(self) -> None:
        expect(self.heading).to_be_visible()
