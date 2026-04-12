"""E2E tests for the wiki page — layout, buttons, header components."""

import httpx
import pytest
from playwright.sync_api import Page, expect

from specivo.testing.e2e_base import create_project
from tests.e2e.e2e_helpers import (
    ConsoleErrorTracker,
    assert_avatar_popup,
    assert_header_search,
)

pytestmark = [pytest.mark.e2e]


def _create_project_with_wiki(api: httpx.Client) -> dict:
    project = create_project(api, prefix="PW")
    api.patch(
        f"/api/v1/projects/{project['key']}/modules/",
        json={"modules": {"wiki": True}},
    )
    return project


@pytest.fixture
def project_data(api_client: httpx.Client) -> dict:
    """Create a project with wiki enabled for all tests in this module."""
    return _create_project_with_wiki(api_client)


@pytest.fixture
def project_key(project_data: dict) -> str:
    return project_data["key"]


def test_no_console_errors(admin_page: Page, project_key: str) -> None:
    """Wiki home page loads without JS console errors."""
    tracker = ConsoleErrorTracker().attach(admin_page)
    admin_page.goto(f"/projects/{project_key}/wiki/home/")
    admin_page.wait_for_load_state("networkidle")
    tracker.assert_no_errors()


def test_wiki_home_renders(admin_page: Page, project_key: str) -> None:
    """Wiki home page shows title and content area."""
    admin_page.goto(f"/projects/{project_key}/wiki/home/")
    admin_page.wait_for_load_state("networkidle")
    expect(admin_page.locator("h1", has_text="Home")).to_be_visible()
    expect(admin_page.locator(".wiki-content")).to_be_visible()


def test_edit_button_visible(admin_page: Page, project_key: str) -> None:
    """Edit button/link is visible on the wiki page."""
    admin_page.goto(f"/projects/{project_key}/wiki/home/")
    admin_page.wait_for_load_state("networkidle")
    expect(admin_page.locator("a, button", has_text="Edit")).to_be_visible()


def test_history_button_visible(admin_page: Page, project_key: str) -> None:
    """History button/link is visible on the wiki page."""
    admin_page.goto(f"/projects/{project_key}/wiki/home/")
    admin_page.wait_for_load_state("networkidle")
    expect(admin_page.locator("a, button", has_text="History")).to_be_visible()


def test_header_search(admin_page: Page, project_key: str) -> None:
    """Global search field is visible in the header."""
    admin_page.goto(f"/projects/{project_key}/wiki/home/")
    assert_header_search(admin_page)


def test_avatar_popup(admin_page: Page, project_key: str) -> None:
    """Avatar dropdown shows Profile, Preferences, API Keys, and Sign out."""
    admin_page.goto(f"/projects/{project_key}/wiki/home/")
    assert_avatar_popup(admin_page)
