"""Page Object Model for the sprint backlog page."""

from __future__ import annotations

from playwright.sync_api import Page, expect


class BacklogPage:
    def __init__(self, page: Page) -> None:
        self.page = page
        self.heading = page.locator("h1", has_text="Backlog")
        self.new_sprint_button = page.locator("button", has_text="New Sprint")
        self.sprint_modal = page.locator(".sp-modal-overlay .sp-modal")

    def navigate(self, project_key: str) -> None:
        self.page.goto(f"/projects/{project_key}/backlog/")

    def expect_loaded(self) -> None:
        expect(self.heading).to_be_visible()
