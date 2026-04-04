"""E2E tests for the Remember Me checkbox on the login page."""

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.pages.login_page import LoginPage

pytestmark = [pytest.mark.e2e]


def test_remember_me_checkbox_visible(page: Page, e2e_server: str) -> None:
    """Login page has a Remember me checkbox."""
    page.goto(f"{e2e_server}/login/")
    checkbox = page.locator("input[type='checkbox']")
    expect(checkbox).to_be_visible()


def test_remember_me_unchecked_by_default(page: Page, e2e_server: str) -> None:
    """Remember me checkbox is unchecked by default."""
    page.goto(f"{e2e_server}/login/")
    checkbox = page.locator("input[type='checkbox']")
    expect(checkbox).not_to_be_checked()


def test_login_with_remember_me_redirects_to_dashboard(page: Page, e2e_server: str, e2e_seed_data: dict) -> None:
    """Login with Remember me checked successfully logs in."""
    page.goto(f"{e2e_server}/login/")
    login = LoginPage(page)
    page.locator("input[type='checkbox']").check()
    login.login(e2e_seed_data["user_login"], e2e_seed_data["password"])
    login.expect_redirected_to_dashboard()
    expect(page).to_have_title("Dashboard - Specivo")


def test_login_without_remember_me_redirects_to_dashboard(page: Page, e2e_server: str, e2e_seed_data: dict) -> None:
    """Login without Remember me successfully logs in."""
    page.goto(f"{e2e_server}/login/")
    login = LoginPage(page)
    # Do NOT check Remember me (default unchecked)
    login.login(e2e_seed_data["user_login"], e2e_seed_data["password"])
    login.expect_redirected_to_dashboard()
    expect(page).to_have_title("Dashboard - Specivo")
