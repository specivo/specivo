"""E2E: create an outgoing webhook in project settings, see it listed, send a test, open the delivery log."""

import re

import httpx
import pytest
from playwright.sync_api import Page, expect

from specivo.testing.e2e_base import create_project
from tests.e2e.e2e_helpers import ConsoleErrorTracker

pytestmark = [pytest.mark.e2e]

_URL = "https://hooks.example.com/e2e-receiver"


@pytest.fixture
def project_key(api_client: httpx.Client) -> str:
    return create_project(api_client)["key"]


def _open_tab(page: Page, project_key: str) -> None:
    page.goto(f"/projects/{project_key}/settings/")
    page.get_by_role("link", name="Webhooks").click()
    expect(page.get_by_test_id("webhooks-card")).to_be_visible()


def test_create_webhook_and_open_delivery_log(admin_page: Page, project_key: str) -> None:
    tracker = ConsoleErrorTracker().attach(admin_page)
    _open_tab(admin_page, project_key)
    expect(admin_page.get_by_text("No webhooks yet")).to_be_visible()

    admin_page.get_by_role("button", name="New webhook").click()
    admin_page.get_by_label("Payload URL").fill(_URL)
    admin_page.get_by_label("Wiki page updated").check()
    admin_page.get_by_role("button", name="Create webhook").click()

    # Listed, and its secret is shown once.
    row = admin_page.get_by_test_id("webhook-row")
    expect(row).to_have_count(1)
    expect(row).to_contain_text(_URL)
    expect(row).to_contain_text("Issue created")
    expect(row).to_contain_text("Wiki page updated")
    expect(admin_page.get_by_test_id("webhook-secret").locator("code")).to_have_text(re.compile(r"^[0-9a-f]{64}$"))

    row.get_by_role("button", name="Send test").click()
    expect(admin_page.get_by_text("Test event queued.")).to_be_visible()

    row.get_by_role("button", name="Delivery log").click()
    log = admin_page.get_by_test_id("webhook-log")
    expect(log).to_be_visible()
    expect(log.locator("tbody tr")).to_have_count(1)
    expect(log.locator("tbody tr").first).to_contain_text("Test event")

    # A reload never shows the secret again.
    _open_tab(admin_page, project_key)
    expect(admin_page.get_by_test_id("webhook-row")).to_contain_text(_URL)
    expect(admin_page.get_by_test_id("webhook-secret")).to_have_count(0)
    tracker.assert_no_errors()


def test_disable_and_delete_webhook(admin_page: Page, project_key: str, api_client: httpx.Client) -> None:
    resp = api_client.post(f"/api/v1/projects/{project_key}/webhooks/", json={"url": _URL, "events": ["issue.created"]})
    assert resp.status_code == 201, resp.text
    _open_tab(admin_page, project_key)

    row = admin_page.get_by_test_id("webhook-row")
    row.get_by_role("button", name="Disable").click()
    expect(row).to_contain_text("disabled")

    row.get_by_role("button", name="Delete").click()
    admin_page.locator(".sp-modal").get_by_role("button", name="Delete webhook").click()
    expect(admin_page.get_by_test_id("webhook-row")).to_have_count(0)
