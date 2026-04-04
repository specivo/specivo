"""Page Object Model for the login page."""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect


class LoginPage:
    def __init__(self, page: Page) -> None:
        self.page = page
        self.login_input = page.locator("#login-field")
        self.password_input = page.locator("#password-field")
        self.submit_button = page.locator("button.sp-btn-login")
        self.error_container = page.locator(".login-error")
        self.error_text = page.locator(".login-error span")
        self.remember_checkbox = page.locator("input[type='checkbox']")

    def navigate(self) -> None:
        self.page.goto("/login/")

    def fill_credentials(self, username: str, password: str) -> None:
        self.login_input.fill(username)
        self.password_input.fill(password)

    def submit(self) -> None:
        self.submit_button.click()

    def login(self, username: str, password: str) -> None:
        self.fill_credentials(username, password)
        self.submit()

    def expect_error_visible(self, text: str | None = None) -> None:
        expect(self.error_container).to_have_class(re.compile(r"show"))
        if text:
            expect(self.error_text).to_contain_text(text)

    def expect_redirected_to_dashboard(self) -> None:
        self.page.wait_for_url("**/", timeout=5000)
