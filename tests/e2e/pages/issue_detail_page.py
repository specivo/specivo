"""Page Object Model for the issue detail page (/issue/{ref}/)."""

from __future__ import annotations

from playwright.sync_api import Page, expect


class IssueDetailPage:
    def __init__(self, page: Page) -> None:
        self.page = page
        # Main content
        self.title = page.locator("h1").first
        self.description_card = page.locator(".description-card").first
        # Tabs (a.activity-tab links)
        self.activity_tab = page.locator("a.activity-tab", has_text="Activity")
        self.attachments_tab = page.locator("a.activity-tab", has_text="Attachments")
        # Description actions
        self.history_link = page.locator("a", has_text="History")
        # Comment form
        self.comment_textarea = page.locator(".comment-form textarea")
        self.comment_submit = page.locator(".comment-form button.sp-btn-primary")

    def navigate(self, issue_ref: str) -> None:
        self.page.goto(f"/issue/{issue_ref}/")

    def expect_loaded(self, subject: str | None = None) -> None:
        expect(self.title).to_be_visible()
        if subject:
            expect(self.title).to_contain_text(subject)
