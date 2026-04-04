"""Page Object Models for wiki pages."""

from __future__ import annotations

from playwright.sync_api import Page, expect


class WikiIndexPage:
    def __init__(self, page: Page) -> None:
        self.page = page
        self.new_page_button = page.locator("a.btn-primary", has_text="New Page")
        self.empty_state = page.locator("text=No wiki pages yet")

    def navigate(self, project_key: str) -> None:
        self.page.goto(f"/projects/{project_key}/wiki/")

    def expect_loaded(self) -> None:
        expect(self.page.locator("h1", has_text="Wiki")).to_be_visible()

    def expect_page_listed(self, title: str) -> None:
        expect(self.page.locator(f"text={title}")).to_be_visible()


class WikiShowPage:
    def __init__(self, page: Page) -> None:
        self.page = page

    def navigate(self, project_key: str, slug: str) -> None:
        self.page.goto(f"/projects/{project_key}/wiki/{slug}/")

    def expect_title(self, title: str) -> None:
        expect(self.page.locator("h1", has_text=title)).to_be_visible()
