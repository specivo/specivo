"""E2E: AI usage in the browser: log it by hand, move unassigned usage, exclude an entry.

The shared admin account logs and moves its own usage in a fresh project per
test, so the assertions look only at that project and at entries the test
wrote (found by their comment).
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import Page, expect

from specivo.testing.e2e_base import create_issue, create_project, unique_key
from tests.e2e.e2e_helpers import ConsoleErrorTracker

pytestmark = [pytest.mark.e2e]


@pytest.fixture
def project(api_client: httpx.Client) -> dict:
    return create_project(api_client, prefix="AIU")


def _log_via_api(api: httpx.Client, project_key: str | None, comments: str, **kw: object) -> dict:
    url = f"/api/v1/projects/{project_key}/ai-usage/" if project_key else "/api/v1/my/ai-usage/"
    body: dict = {
        "tool": "claude-code",
        "model": "claude-sonnet-4-5",
        "usage_date": "2026-03-10",
        "tokens": {"input": 120_000, "output": 8_000},
        "cost_type": "api",
        "comments": comments,
        **kw,
    }
    resp = api.post(url, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _entries(api: httpx.Client, project_key: str) -> list[dict]:
    resp = api.get(f"/api/v1/projects/{project_key}/ai-usage/")
    assert resp.status_code == 200, resp.text
    return resp.json()["items"]


def test_log_ai_usage_by_hand_on_an_issue(admin_page: Page, api_client: httpx.Client, project: dict) -> None:
    issue = create_issue(api_client, project["key"], "Wire the payment form")
    comment = unique_key("Pairing session ")
    console = ConsoleErrorTracker().attach(admin_page)

    admin_page.goto(f"/issue/{issue['key']}/#ai-usage")
    panel = admin_page.get_by_test_id("ai-usage-tab-panel")
    expect(panel.get_by_test_id("aiu-empty")).to_be_visible()
    panel.get_by_test_id("aiu-issue-log").click()

    expect(admin_page.get_by_test_id("aiu-form-issue")).to_have_value(issue["key"])
    admin_page.get_by_test_id("aiu-form-model").fill("claude-sonnet-4-5")
    admin_page.get_by_test_id("aiu-form-input").fill("250000")
    admin_page.get_by_test_id("aiu-form-output").fill("12000")
    admin_page.get_by_test_id("aiu-form-cost-type").select_option("api")
    admin_page.get_by_test_id("aiu-form-comments").fill(comment)
    admin_page.get_by_test_id("aiu-form-submit").click()

    # Back on the issue, on the AI usage tab, with the new entry.
    admin_page.wait_for_url(f"**/issue/{issue['key']}/**")
    panel = admin_page.get_by_test_id("ai-usage-tab-panel")
    expect(panel.get_by_test_id("aiu-issue-session")).to_have_count(1)
    expect(panel.get_by_test_id("aiu-issue-tokens")).to_have_attribute("title", "262,000")

    entries = [e for e in _entries(api_client, project["key"]) if e["comments"] == comment]
    assert len(entries) == 1
    assert entries[0]["issue_key"] == issue["key"] and entries[0]["source"] == "manual"
    console.assert_no_errors()


def test_issue_tab_pages_and_charts_without_reload(admin_page: Page, api_client: httpx.Client, project: dict) -> None:
    """Twelve entries over six days: totals over all of them, a chart, and the sessions a page at a time."""
    issue = create_issue(api_client, project["key"], "Rebuild the checkout")
    for i in range(12):
        _log_via_api(
            api_client, project["key"], f"Session {i}", issue_id=issue["id"], usage_date=f"2026-03-{10 + i % 6:02d}"
        )
    console = ConsoleErrorTracker().attach(admin_page)

    admin_page.goto(f"/issue/{issue['key']}/#ai-usage")
    panel = admin_page.get_by_test_id("ai-usage-tab-panel")
    expect(panel.get_by_test_id("aiu-issue-kpis")).to_contain_text("12 entries")
    expect(panel.locator("#aiu-issue-chart-time[data-sp-drawn]")).to_have_count(1)  # the bundle came with the tab
    expect(panel.get_by_test_id("aiu-issue-session")).to_have_count(10)

    url = admin_page.url
    panel.get_by_test_id("aiu-issue-next").click()
    expect(panel.get_by_test_id("aiu-issue-range")).to_have_text("11–12 of 12")
    expect(panel.get_by_test_id("aiu-issue-session")).to_have_count(2)
    expect(panel.get_by_test_id("aiu-issue-kpis")).to_contain_text("12 entries")
    expect(panel.locator("#aiu-issue-chart-time[data-sp-drawn]")).to_have_count(1)
    expect(admin_page.locator("#aiu-issue-sessions-title")).to_be_focused()
    assert admin_page.url == url  # swapped in place; the issue page stays
    console.assert_no_errors()


def test_move_unassigned_usage_to_an_issue(admin_page: Page, api_client: httpx.Client, project: dict) -> None:
    issue = create_issue(api_client, project["key"], "Speed up the export")
    comment = unique_key("Unassigned session ")
    entry = _log_via_api(api_client, None, comment)

    admin_page.goto("/my/ai-usage/?project=-")
    row = admin_page.locator(f'tr[data-entry-id="{entry["id"]}"]')
    expect(row).to_be_visible()
    row.locator('input[name="entry_ids"]').check()
    admin_page.get_by_test_id("aiu-reassign-issue").fill(issue["key"])
    admin_page.get_by_test_id("aiu-reassign-submit").click()

    expect(admin_page.get_by_test_id("aiu-notice")).to_be_visible()
    expect(admin_page.locator(f'tr[data-entry-id="{entry["id"]}"]')).to_have_count(0)

    moved = [e for e in _entries(api_client, project["key"]) if e["id"] == entry["id"]]
    assert len(moved) == 1 and moved[0]["issue_key"] == issue["key"]


def test_select_all_ticks_every_row(admin_page: Page, api_client: httpx.Client) -> None:
    for n in range(2):
        _log_via_api(api_client, None, unique_key(f"Bulk {n} "))
    admin_page.goto("/my/ai-usage/?project=-")
    admin_page.locator('input[data-select-all="entry_ids"]').check()
    boxes = admin_page.locator('input[name="entry_ids"]')
    assert boxes.count() >= 2
    for i in range(boxes.count()):
        expect(boxes.nth(i)).to_be_checked()


def test_exclude_and_include_an_entry(admin_page: Page, api_client: httpx.Client, project: dict) -> None:
    kept = _log_via_api(api_client, project["key"], unique_key("Kept "))
    dropped = _log_via_api(api_client, project["key"], unique_key("Dropped "))

    admin_page.goto(f"/projects/{project['key']}/ai-usage/")
    expect(admin_page.get_by_test_id("aiu-total-entries")).to_have_text("2")
    admin_page.locator(f'tr[data-entry-id="{dropped["id"]}"]').get_by_test_id("aiu-exclude").click()

    expect(admin_page.get_by_test_id("aiu-notice")).to_be_visible()
    row = admin_page.locator(f'tr[data-entry-id="{dropped["id"]}"]')
    expect(row.get_by_test_id("aiu-excluded-badge")).to_be_visible()
    expect(admin_page.get_by_test_id("aiu-total-entries")).to_have_text("1")
    expect(admin_page.locator(f'tr[data-entry-id="{kept["id"]}"]').get_by_test_id("aiu-excluded-badge")).to_have_count(
        0
    )

    row.get_by_test_id("aiu-include").click()
    expect(admin_page.get_by_test_id("aiu-total-entries")).to_have_text("2")
    assert all(not e["excluded"] for e in _entries(api_client, project["key"]))
