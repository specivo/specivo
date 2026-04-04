"""Page Object Model for the issue list page."""

from __future__ import annotations

from playwright.sync_api import Page, expect


class IssueListPage:
    def __init__(self, page: Page) -> None:
        self.page = page
        self.new_issue_button = page.locator("a.sp-btn-primary, a.btn-primary", has_text="New Issue")
        self.status_filter = page.locator("select[name='status']")
        self.tracker_filter = page.locator("select[name='tracker_id']")
        self.apply_button = page.locator("button.sp-btn-ghost, button.btn-ghost", has_text="Apply")
        self.issue_rows = page.locator(".issue-table tbody tr")
        self.empty_state = page.locator("text=No issues found")

    def navigate(self, project_key: str) -> None:
        self.page.goto(f"/projects/{project_key}/issues/")

    def expect_loaded(self) -> None:
        expect(self.page.locator("h1", has_text="Issues")).to_be_visible()

    def expect_issue_visible(self, subject: str) -> None:
        expect(self.page.locator(f".issue-table td a:has-text('{subject}')")).to_be_visible()

    def filter_by_status(self, status: str) -> None:
        self.status_filter.select_option(status)
        self.apply_button.click()
        self.page.wait_for_load_state("networkidle")

    def click_new_issue(self) -> None:
        self.new_issue_button.click()
