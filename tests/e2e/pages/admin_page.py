"""Page Object Model for admin pages."""

from __future__ import annotations

from playwright.sync_api import Page, expect


class AdminDashboardPage:
    def __init__(self, page: Page) -> None:
        self.page = page
        self.stat_cards = page.locator(".stat-card")

    def navigate(self) -> None:
        self.page.goto("/admin/")

    def expect_loaded(self) -> None:
        expect(self.page.locator("h1", has_text="Admin Dashboard")).to_be_visible()
        expect(self.stat_cards.first).to_be_visible()

    def expect_stat_visible(self, label: str) -> None:
        expect(self.page.locator(f".stat-label:has-text('{label}')")).to_be_visible()
