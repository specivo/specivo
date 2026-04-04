"""Page Object Model for the issue create/edit form (Alpine.js issueForm)."""

from __future__ import annotations

from playwright.sync_api import Page, expect


class IssueFormPage:
    def __init__(self, page: Page) -> None:
        self.page = page
        self.subject_input = page.locator("#subject")
        self.tracker_select = page.locator("#tracker")
        self.description_input = page.locator("#description")
        self.priority_select = page.locator("#priority")
        self.submit_button = page.locator("button.btn-primary")
        self.cancel_link = page.locator("a.cancel-link")

    def navigate_new(self, project_key: str) -> None:
        self.page.goto(f"/projects/{project_key}/issues/new/")

    def fill_subject(self, subject: str) -> None:
        # Use press_sequentially to trigger Alpine.js x-model input events
        self.subject_input.click()
        self.subject_input.press_sequentially(subject, delay=10)

    def fill_description(self, description: str) -> None:
        self.description_input.fill(description)

    def select_tracker(self, tracker_name: str) -> None:
        self.tracker_select.select_option(label=tracker_name)

    def submit(self) -> None:
        self.submit_button.click()

    def expect_loaded(self, mode: str = "create") -> None:
        title = "New Issue" if mode == "create" else "Edit"
        expect(self.page.locator("h1", has_text=title)).to_be_visible()
        expect(self.subject_input).to_be_visible()
