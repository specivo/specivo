"""E2E tests for the /login/ page — form, auth flow, console errors."""

from __future__ import annotations

import pytest
from playwright.sync_api import Browser, Page, expect

from tests.e2e.e2e_helpers import ConsoleErrorTracker
from tests.e2e.pages.login_page import LoginPage

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
    """Login page loads without JS console errors."""
    tracker = ConsoleErrorTracker().attach(anon_page)
    anon_page.goto("/login/")
    anon_page.wait_for_load_state("networkidle")
    tracker.assert_no_errors()


# -- Form visibility ---------------------------------------------------------


def test_form_fields_visible(anon_page: Page) -> None:
    """Login, password, and submit button are visible."""
    anon_page.goto("/login/")
    login = LoginPage(anon_page)
    expect(login.login_input).to_be_visible()
    expect(login.password_input).to_be_visible()
    expect(login.submit_button).to_be_visible()


def test_remember_me_checkbox_visible(anon_page: Page) -> None:
    """Remember-me checkbox exists and is unchecked by default."""
    anon_page.goto("/login/")
    login = LoginPage(anon_page)
    expect(login.remember_checkbox).to_be_visible()
    expect(login.remember_checkbox).not_to_be_checked()


def test_forgot_password_link(anon_page: Page) -> None:
    """A link to /forgot-password/ is present on the login page."""
    anon_page.goto("/login/")
    link = anon_page.locator("a[href='/forgot-password/']")
    expect(link).to_be_visible()


# -- Auth flows --------------------------------------------------------------


def test_login_success_redirects_to_dashboard(anon_page: Page) -> None:
    """Valid credentials redirect to the dashboard."""
    anon_page.goto("/login/")
    login = LoginPage(anon_page)
    login.login("e2e_admin", "testpassword")
    login.expect_redirected_to_dashboard()


def test_login_invalid_credentials_shows_error(anon_page: Page) -> None:
    """Wrong password shows an error message."""
    anon_page.goto("/login/")
    login = LoginPage(anon_page)
    login.login("e2e_admin", "wrongpassword")
    login.expect_error_visible("Invalid login or password")


def test_already_authenticated_shows_widget(auth_page: Page) -> None:
    """Authenticated user visiting /login/ sees 'already signed in' widget."""
    auth_page.goto("/login/")
    expect(auth_page.locator("text=You are already signed in")).to_be_visible()


def test_unauthenticated_redirect_to_login(anon_page: Page) -> None:
    """Visiting / without auth redirects to /login/."""
    anon_page.goto("/")
    anon_page.wait_for_url("**/login/")
    expect(anon_page.locator("#login-field")).to_be_visible()
