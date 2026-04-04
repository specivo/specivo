"""Page Object Model for the dashboard page."""

from __future__ import annotations

from playwright.sync_api import Page, expect


class DashboardPage:
    def __init__(self, page: Page) -> None:
        self.page = page
        self.status_banner = page.locator(".status-banner")
        self.project_cards = page.locator(".project-compact")
        self.sidebar = page.locator("nav.sidebar")
        self.sidebar_brand = page.locator(".sidebar-brand")

    def navigate(self) -> None:
        self.page.goto("/")

    def expect_loaded(self) -> None:
        expect(self.page).to_have_title("Dashboard - Specivo")
        expect(self.status_banner).to_be_visible()

    def expect_project_visible(self, project_key: str) -> None:
        expect(self.page.locator(f"text={project_key}").first).to_be_visible()
