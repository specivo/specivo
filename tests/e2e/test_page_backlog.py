"""E2E tests for the backlog page — layout, sprint creation modal."""

import httpx
import pytest
from playwright.sync_api import Page, expect

from specivo.testing.e2e_base import create_project
from tests.e2e.e2e_helpers import (
    ConsoleErrorTracker,
    assert_header_search,
)

pytestmark = [pytest.mark.e2e]


@pytest.fixture
def project_data(api_client: httpx.Client) -> dict:
    """Create a project via API for all tests in this module."""
    return create_project(api_client, prefix="PB")


@pytest.fixture
def project_key(project_data: dict) -> str:
    return project_data["key"]


def test_no_console_errors(admin_page: Page, project_key: str) -> None:
    """Backlog page loads without JS console errors."""
    tracker = ConsoleErrorTracker().attach(admin_page)
    admin_page.goto(f"/projects/{project_key}/backlog/")
    admin_page.wait_for_load_state("networkidle")
    tracker.assert_no_errors()


def test_backlog_renders(admin_page: Page, project_key: str) -> None:
    """Backlog page shows h1 with 'Backlog'."""
    admin_page.goto(f"/projects/{project_key}/backlog/")
    admin_page.wait_for_load_state("networkidle")
    expect(admin_page.locator("h1", has_text="Backlog")).to_be_visible()


def test_new_sprint_button(admin_page: Page, project_key: str) -> None:
    """'New Sprint' button is visible on the backlog page."""
    admin_page.goto(f"/projects/{project_key}/backlog/")
    admin_page.wait_for_load_state("networkidle")
    expect(admin_page.locator("button, a", has_text="New Sprint")).to_be_visible()


def test_create_sprint_modal_opens(admin_page: Page, project_key: str) -> None:
    """Clicking 'New Sprint' opens a modal with a name input."""
    admin_page.goto(f"/projects/{project_key}/backlog/")
    admin_page.wait_for_load_state("networkidle")

    admin_page.click("text=New Sprint")

    modal = admin_page.locator(".sp-modal-overlay .sp-modal")
    expect(modal).to_be_visible()
    expect(modal.locator("input[type='text']")).to_be_visible()


def test_header_search(admin_page: Page, project_key: str) -> None:
    """Global search field is visible in the header."""
    admin_page.goto(f"/projects/{project_key}/backlog/")
    assert_header_search(admin_page)
