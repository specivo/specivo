"""E2E tests for the project overview page — layout, sidebar, header components."""

import httpx
import pytest
from playwright.sync_api import Page, expect

from specivo.testing.e2e_base import create_project
from tests.e2e.e2e_helpers import (
    ConsoleErrorTracker,
    assert_avatar_popup,
    assert_header_search,
    assert_project_sidebar,
)

pytestmark = [pytest.mark.e2e]


@pytest.fixture
def project_data(api_client: httpx.Client) -> dict:
    """Create a project via API for all tests in this module."""
    return create_project(api_client)


@pytest.fixture
def project_key(project_data: dict) -> str:
    return project_data["key"]


@pytest.fixture
def project_name(project_data: dict) -> str:
    return project_data["name"]


def test_no_console_errors(admin_page: Page, project_key: str) -> None:
    """Project overview loads without JS console errors."""
    tracker = ConsoleErrorTracker().attach(admin_page)
    admin_page.goto(f"/projects/{project_key}/")
    admin_page.wait_for_load_state("networkidle")
    tracker.assert_no_errors()


def test_project_overview_renders(admin_page: Page, project_key: str, project_name: str) -> None:
    """Project overview page shows the project name in h1."""
    admin_page.goto(f"/projects/{project_key}/")
    expect(admin_page.locator("h1")).to_contain_text(project_name)


def test_project_sidebar_items(admin_page: Page, project_key: str) -> None:
    """Project sidebar shows Issues, Backlog, Wiki, Roadmap, etc."""
    admin_page.goto(f"/projects/{project_key}/")
    assert_project_sidebar(admin_page, project_key)


def test_header_search_field(admin_page: Page, project_key: str) -> None:
    """Global search field is visible in the header."""
    admin_page.goto(f"/projects/{project_key}/")
    assert_header_search(admin_page)


def test_avatar_popup_links(admin_page: Page, project_key: str) -> None:
    """Avatar dropdown shows Profile, Preferences, API Keys, and Sign out."""
    admin_page.goto(f"/projects/{project_key}/")
    assert_avatar_popup(admin_page)
