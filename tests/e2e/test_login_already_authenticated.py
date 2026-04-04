"""E2E tests for login page when user is already authenticated."""

import pytest
from playwright.sync_api import Page, expect

pytestmark = [pytest.mark.e2e]


def test_login_page_shows_already_signed_in(auth_page: Page) -> None:
    """Authenticated user visiting /login/ sees 'already signed in' widget."""
    auth_page.goto("/login/")
    expect(auth_page.locator("text=You are already signed in")).to_be_visible()


def test_login_page_shows_username(auth_page: Page, e2e_seed_data: dict) -> None:
    """Already signed in widget shows the user's login name."""
    auth_page.goto("/login/")
    expect(auth_page.locator(f"text={e2e_seed_data['user_login']}")).to_be_visible()


def test_login_page_has_dashboard_link(auth_page: Page) -> None:
    """Already signed in widget has a 'Go to Dashboard' link."""
    auth_page.goto("/login/")
    dashboard_link = auth_page.locator("a[href='/']", has_text="Dashboard")
    expect(dashboard_link).to_be_visible()


def test_login_page_has_logout_link(auth_page: Page) -> None:
    """Already signed in widget has a 'Sign out' link."""
    auth_page.goto("/login/")
    logout_link = auth_page.locator("a[href='/logout/']")
    expect(logout_link).to_be_visible()


def test_login_page_dashboard_link_works(auth_page: Page) -> None:
    """Clicking 'Go to Dashboard' navigates to /."""
    auth_page.goto("/login/")
    auth_page.locator("a[href='/']", has_text="Dashboard").click()
    auth_page.wait_for_url("**/")
    expect(auth_page).to_have_title("Dashboard - Specivo")


def test_login_page_logout_link_works(auth_page: Page) -> None:
    """Clicking 'Sign out' logs out and shows login form."""
    auth_page.goto("/login/")
    auth_page.locator("a[href='/logout/']").click()
    auth_page.wait_for_url("**/login/")
    # After logout, should see the login form, not the already-signed-in widget
    expect(auth_page.locator("#login-field")).to_be_visible()


def test_unauthenticated_sees_login_form(page: Page, e2e_server: str) -> None:
    """Unauthenticated user sees the normal login form."""
    page.goto(f"{e2e_server}/login/")
    expect(page.locator("#login-field")).to_be_visible()
    expect(page.locator("text=You are already signed in")).not_to_be_visible()
