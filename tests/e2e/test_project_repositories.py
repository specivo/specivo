"""E2E: connect a repository in project settings, copy the URL and secret, then see a ping arrive."""

import hashlib
import hmac
import json
import re
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest
from playwright.sync_api import BrowserContext, Page, expect

from specivo.testing.e2e_base import create_project
from tests.e2e.e2e_helpers import ConsoleErrorTracker

pytestmark = [pytest.mark.e2e]

_REPO = "example-org/widgets"
_PING = Path(__file__).resolve().parents[1] / "fixtures" / "forge" / "github_ping.json"


@pytest.fixture
def project_key(api_client: httpx.Client) -> str:
    return create_project(api_client)["key"]


def _open_tab(page: Page, project_key: str) -> None:
    page.goto(f"/projects/{project_key}/settings/")
    page.get_by_role("link", name="Repositories").click()
    expect(page.get_by_test_id("repositories-card")).to_be_visible()


def test_connect_repository_then_receive_ping(
    admin_context: BrowserContext, admin_page: Page, project_key: str, api_client: httpx.Client
) -> None:
    admin_context.grant_permissions(["clipboard-read", "clipboard-write"])
    tracker = ConsoleErrorTracker().attach(admin_page)
    _open_tab(admin_page, project_key)
    expect(admin_page.get_by_text("No repositories connected")).to_be_visible()

    admin_page.get_by_role("button", name="Connect a repository").first.click()
    admin_page.get_by_label("Provider").select_option("github")
    admin_page.get_by_label("Repository", exact=True).fill(_REPO)
    admin_page.get_by_label("Repository URL").fill(f"https://github.example.com/{_REPO}")
    admin_page.get_by_test_id("repository-save").click()

    # Listed as never received; the URL and secret are shown once, with GitHub's setup steps.
    row = admin_page.get_by_test_id("repository-row")
    expect(row).to_have_count(1)
    expect(row).to_contain_text(_REPO)
    expect(row.get_by_test_id("repository-last-delivery")).to_have_text("Never received")
    reveal = admin_page.get_by_test_id("repository-secret")
    expect(reveal).to_contain_text("Just the push event")
    hook_url = admin_page.get_by_test_id("repository-hook-url").inner_text()
    secret = admin_page.get_by_test_id("repository-secret-value").inner_text()
    assert re.fullmatch(r"[0-9a-f]{64}", secret)
    assert re.search(r"/hooks/github/[0-9a-f-]{36}/$", hook_url)

    reveal.get_by_role("button", name="Copy").nth(1).click()
    expect(reveal.get_by_role("button", name="Copied")).to_be_visible()
    assert admin_page.evaluate("navigator.clipboard.readText()") == secret

    # The forge sends its ping.
    payload = json.loads(_PING.read_text())
    payload["repository"]["full_name"] = _REPO
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    resp = httpx.post(
        str(api_client.base_url).rstrip("/") + urlparse(hook_url).path,
        content=body,
        headers={"X-GitHub-Event": "ping", "X-GitHub-Delivery": "e2e-ping-1", "X-Hub-Signature-256": signature},
        timeout=10,
    )
    assert resp.status_code == 200, resp.text

    # A reload shows the delivery, never the secret.
    _open_tab(admin_page, project_key)
    expect(admin_page.get_by_test_id("repository-secret")).to_have_count(0)
    row = admin_page.get_by_test_id("repository-row")
    expect(row.get_by_test_id("repository-last-delivery")).not_to_have_text("Never received")
    row.get_by_role("button", name="Delivery log").click()
    log = admin_page.get_by_test_id("repository-log")
    expect(log.locator("tbody tr")).to_have_count(1)
    expect(log.locator("tbody tr").first).to_contain_text("ping")
    expect(log.locator("tbody tr").first).to_contain_text("Processed")
    tracker.assert_no_errors()


def test_disable_and_disconnect(admin_page: Page, project_key: str, api_client: httpx.Client) -> None:
    resp = api_client.post(
        f"/api/v1/projects/{project_key}/repositories/",
        json={"provider": "forgejo", "repo_full_name": _REPO, "repo_url": f"https://forgejo.example.org/{_REPO}"},
    )
    assert resp.status_code == 201, resp.text
    _open_tab(admin_page, project_key)

    row = admin_page.get_by_test_id("repository-row")
    expect(row).to_contain_text("Forgejo")
    row.get_by_role("button", name="Disable").click()
    expect(row).to_contain_text("disabled")

    row.get_by_role("button", name="New secret").click()
    expect(admin_page.get_by_test_id("repository-secret")).to_contain_text("Forgejo")

    row.get_by_role("button", name="Delete").click()
    admin_page.locator(".sp-modal").get_by_role("button", name="Disconnect").click()
    expect(admin_page.get_by_test_id("repository-row")).to_have_count(0)
