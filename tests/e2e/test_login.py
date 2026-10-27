"""E2E tests for the login page (Alpine.js loginForm component)."""

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.pages.login_page import LoginPage

pytestmark = [pytest.mark.e2e]


def test_login_page_renders(page: Page, e2e_server: str) -> None:
    """Login page loads with form fields and submit button."""
    page.goto(f"{e2e_server}/login/")
    login = LoginPage(page)
    expect(login.login_input).to_be_visible()
    expect(login.password_input).to_be_visible()
    expect(login.submit_button).to_be_visible()
    expect(login.submit_button).to_contain_text("Sign in")


def test_login_page_has_alpine_component(page: Page, e2e_server: str) -> None:
    """Login page initializes Alpine.js loginForm component."""
    page.goto(f"{e2e_server}/login/")
    expect(page.locator("[x-data='loginForm']")).to_be_visible()


def test_login_success_redirects_to_dashboard(page: Page, e2e_server: str, e2e_seed_data: dict) -> None:
    """Valid credentials redirect to the dashboard."""
    page.goto(f"{e2e_server}/login/")
    login = LoginPage(page)
    login.login(e2e_seed_data["user_login"], e2e_seed_data["password"])
    login.expect_redirected_to_dashboard()
    expect(page).to_have_title("Dashboard - Specivo")


def test_login_invalid_credentials_shows_error(page: Page, e2e_server: str) -> None:
    """Invalid credentials show an Alpine.js error message."""
    page.goto(f"{e2e_server}/login/")
    login = LoginPage(page)
    login.login("nonexistent", "wrongpass")
    login.expect_error_visible("Invalid login or password")


def test_unauthenticated_redirects_to_login(page: Page, e2e_server: str) -> None:
    """Navigating to / without auth redirects to /login/."""
    page.goto(f"{e2e_server}/")
    page.wait_for_url("**/login/")
    expect(page.locator("#login-field")).to_be_visible()


def test_header_sign_out_lands_on_login(signout_page: Page) -> None:
    """The header's Sign out button signs out and lands on /login/."""
    signout_page.goto("/")
    signout_page.locator(".header-user").click()
    signout_page.locator(".user-dropdown form[action='/logout/'] button[type='submit']").click()
    signout_page.wait_for_url("**/login/")
    expect(signout_page.locator("#login-field")).to_be_visible()

    # After logout, navigating to dashboard should redirect back to login
    signout_page.goto("/")
    signout_page.wait_for_url("**/login/")


def test_logout_url_asks_before_signing_out(signout_page: Page) -> None:
    """A bookmarked /logout/ shows a confirmation; only its button signs out."""
    signout_page.goto("/logout/")
    expect(signout_page.locator("text=Sign out?")).to_be_visible()

    # Still signed in: the GET changed nothing.
    signout_page.goto("/")
    expect(signout_page).to_have_title("Dashboard - Specivo")

    signout_page.goto("/logout/")
    signout_page.locator("[data-testid='logout-confirm']").click()
    signout_page.wait_for_url("**/login/")
    expect(signout_page.locator("#login-field")).to_be_visible()
