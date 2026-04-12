"""E2E tests for the /forgot-password/ page — form, links, console errors."""

from __future__ import annotations

import pytest
from playwright.sync_api import Browser, Page, expect

from tests.e2e.e2e_helpers import ConsoleErrorTracker
from tests.e2e.pages.forgot_password_page import ForgotPasswordPage

pytestmark = [pytest.mark.e2e]


@pytest.fixture
def anon_page(browser: Browser, e2e_base_url: str):
    """Fresh browser context with no auth cookies."""
    context = browser.new_context(base_url=e2e_base_url)
    page = context.new_page()
    yield page
    context.close()


# -- Console errors ----------------------------------------------------------


def test_no_console_errors(anon_page: Page) -> None:
    """Forgot-password page loads without JS console errors."""
    tracker = ConsoleErrorTracker().attach(anon_page)
    anon_page.goto("/forgot-password/")
    anon_page.wait_for_load_state("networkidle")
    tracker.assert_no_errors()


# -- Form visibility ---------------------------------------------------------


def test_form_visible(anon_page: Page) -> None:
    """Email input and submit button are visible."""
    fp = ForgotPasswordPage(anon_page)
    fp.navigate()
    expect(fp.email_input).to_be_visible()
    expect(fp.submit_button).to_be_visible()


def test_back_to_login_link(anon_page: Page) -> None:
    """A link back to /login/ is visible."""
    fp = ForgotPasswordPage(anon_page)
    fp.navigate()
    expect(fp.back_to_login).to_be_visible()
