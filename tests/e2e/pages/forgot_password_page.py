"""Page Object Model for the forgot-password page."""

from __future__ import annotations

from playwright.sync_api import Page, expect


class ForgotPasswordPage:
    def __init__(self, page: Page) -> None:
        self.page = page
        self.email_input = page.locator("input[type='email'], input[x-model='email']")
        self.submit_button = page.locator("button[type='submit'], button.sp-btn-login")
        self.back_to_login = page.locator(".login-footer a[href='/login/']")

    def navigate(self) -> None:
        self.page.goto("/forgot-password/")

    def expect_loaded(self) -> None:
        expect(self.email_input).to_be_visible()
        expect(self.submit_button).to_be_visible()
