"""E2E: "Copy branch name" on the issue page copies the server-built name and says so."""

import httpx
import pytest
from playwright.sync_api import BrowserContext, Page, expect

from specivo.testing.e2e_base import create_issue, create_project
from tests.e2e.e2e_helpers import ConsoleErrorTracker

pytestmark = [pytest.mark.e2e]


@pytest.fixture
def issue(api_client: httpx.Client) -> dict:
    project = create_project(api_client)
    return create_issue(api_client, project["key"], "Login fails on Safari!")


def test_click_copies_and_shows_feedback(admin_context: BrowserContext, admin_page: Page, issue: dict) -> None:
    admin_context.grant_permissions(["clipboard-read", "clipboard-write"])
    tracker = ConsoleErrorTracker().attach(admin_page)
    admin_page.goto(f"/issue/{issue['key']}/")

    name = admin_page.get_by_test_id("branch-name").input_value()
    assert name.endswith(f"/{issue['key']}-login-fails-on-safari")

    admin_page.get_by_test_id("copy-branch-name").click()
    expect(admin_page.get_by_role("status").filter(has_text="Copied")).to_be_visible()
    assert admin_page.evaluate("navigator.clipboard.readText()") == name
    tracker.assert_no_errors()


def test_keyboard_activation(admin_context: BrowserContext, admin_page: Page, issue: dict) -> None:
    admin_context.grant_permissions(["clipboard-read", "clipboard-write"])
    admin_page.goto(f"/issue/{issue['key']}/")
    button = admin_page.get_by_test_id("copy-branch-name")
    button.focus()
    admin_page.keyboard.press("Enter")
    expect(admin_page.get_by_role("status").filter(has_text="Copied")).to_be_visible()


def test_without_clipboard_the_name_is_selected(admin_page: Page, issue: dict) -> None:
    # No clipboard permission and the API removed: the fallback selects the text.
    admin_page.add_init_script("Object.defineProperty(navigator, 'clipboard', { value: undefined });")
    admin_page.goto(f"/issue/{issue['key']}/")
    admin_page.get_by_test_id("copy-branch-name").click()
    expect(admin_page.get_by_role("status").filter(has_text="Press Ctrl+C to copy")).to_be_visible()
    selected = admin_page.evaluate("window.getSelection().toString() || document.activeElement.value")
    assert selected == admin_page.get_by_test_id("branch-name").input_value()
