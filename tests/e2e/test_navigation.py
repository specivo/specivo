"""E2E tests for navigation: sidebar, breadcrumbs, responsive layout."""

import pytest
from playwright.sync_api import Page, expect

pytestmark = [pytest.mark.e2e]


def test_sidebar_dashboard_link(auth_page: Page) -> None:
    """Clicking Dashboard in sidebar navigates to /."""
    auth_page.goto("/")
    link = auth_page.locator("nav.sidebar a.sidebar-item", has_text="Dashboard")
    expect(link).to_be_visible()
    link.click()
    auth_page.wait_for_url("**/")


def test_sidebar_projects_link(auth_page: Page) -> None:
    """Clicking Projects in sidebar navigates to /projects/."""
    auth_page.goto("/")
    link = auth_page.locator("nav.sidebar a.sidebar-item", has_text="Projects")
    expect(link).to_be_visible()
    link.click()
    auth_page.wait_for_url("**/projects/")


def test_sidebar_brand_links_to_home(auth_page: Page) -> None:
    """Clicking the Specivo brand logo navigates to dashboard."""
    auth_page.goto("/projects/")
    auth_page.locator(".sidebar-brand").click()
    auth_page.wait_for_url("**/")


@pytest.mark.skip(reason="sidebar admin link rendering depends on template context — tracked separately")
def test_admin_sidebar_visible_for_admin(admin_page: Page) -> None:
    """Admin users see the Admin link in the sidebar."""
    admin_page.goto("/")
    admin_link = admin_page.locator("nav.sidebar a[href='/admin/']")
    expect(admin_link).to_be_visible()


def test_admin_sidebar_hidden_for_regular_user(auth_page: Page) -> None:
    """Regular users do not see the Admin link in the sidebar."""
    auth_page.goto("/")
    admin_link = auth_page.locator("nav.sidebar a.sidebar-item", has_text="Admin")
    expect(admin_link).not_to_be_visible()
