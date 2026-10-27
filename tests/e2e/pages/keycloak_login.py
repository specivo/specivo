"""Page Object Model for the disposable Keycloak's login form (SSO E2E tests only)."""

from __future__ import annotations

from playwright.sync_api import Page


class KeycloakLoginPage:
    def __init__(self, page: Page) -> None:
        self.page = page
        self.username_input = page.locator("#username")
        self.password_input = page.locator("#password")
        self.submit_button = page.locator("#kc-login")

    def sign_in(self, username: str, password: str) -> None:
        self.username_input.fill(username)
        self.password_input.fill(password)
        self.submit_button.click()
