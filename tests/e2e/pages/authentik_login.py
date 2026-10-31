"""Page Object Model for the disposable authentik's sign-in flow (SSO E2E tests only).

authentik's pages are web components; Playwright's CSS locators reach into
their open shadow roots. The default authentication flow asks for the
username first and the password on a second stage, which replaces the first:
input typed before it has rendered is lost, so each stage is waited for.
"""

from __future__ import annotations

from playwright.sync_api import Page


class AuthentikLoginPage:
    def __init__(self, page: Page) -> None:
        self.page = page
        self.identification = page.locator("ak-stage-identification")
        self.password_stage = page.locator("ak-stage-password")
        self.username_input = self.identification.locator('input[name="uidField"]')

    def sign_in(self, username: str, password: str) -> None:
        self.username_input.fill(username)
        self.identification.locator('button[type="submit"]').click()
        self.password_stage.wait_for()
        field = self.password_stage.locator('input[name="password"]')
        field.fill(password)
        self.password_stage.locator('button[type="submit"]').click()
