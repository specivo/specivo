"""E2E: the AI spend reports in the browser: the chart draws, presets and grouping work, cells drill through.

The shared admin account logs its own usage in a fresh project per test, so
the project report shows only what the test wrote.
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import Page, expect

from specivo.testing.e2e_base import create_project, unique_key
from tests.e2e.e2e_helpers import ConsoleErrorTracker

pytestmark = [pytest.mark.e2e]

MARCH = "from_date=2026-03-01&to_date=2026-03-31"


@pytest.fixture
def project(api_client: httpx.Client) -> dict:
    return create_project(api_client, prefix="ASR")


def _log(api: httpx.Client, project_key: str, model: str, day: str, amount: str, comment: str) -> dict:
    resp = api.post(
        f"/api/v1/projects/{project_key}/ai-usage/",
        json={
            "tool": "claude-code",
            "model": model,
            "usage_date": day,
            "tokens": {"input": 100_000, "output": 5_000},
            "cost_type": "api",
            "cost": {"amount": amount, "currency": "USD"},
            "comments": comment,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_project_report_chart_and_drill_through(admin_page: Page, api_client: httpx.Client, project: dict) -> None:
    key = project["key"]
    comment = unique_key("Report session ")
    _log(api_client, key, "claude-sonnet-4-5", "2026-03-03", "1.50", comment)
    _log(api_client, key, "claude-sonnet-4-5", "2026-03-12", "2.00", comment)
    _log(api_client, key, "claude-haiku-4-5", "2026-03-12", "0.25", comment)
    console = ConsoleErrorTracker().attach(admin_page)

    admin_page.goto(f"/projects/{key}/ai-usage/")
    admin_page.get_by_test_id("aiu-report-link").click()
    admin_page.wait_for_url(f"**/projects/{key}/ai-usage/report/**")
    expect(admin_page.get_by_test_id("spend-presets")).to_be_visible()

    admin_page.goto(f"/projects/{key}/ai-usage/report/?{MARCH}&group=model&period=week")
    chart = admin_page.get_by_test_id("spend-chart")
    expect(chart).to_have_attribute("data-sp-drawn", "")
    expect(chart.locator("svg").first).to_be_visible()
    rows = admin_page.get_by_test_id("spend-row")
    expect(rows).to_have_count(3)  # sonnet in two weeks, haiku in one
    expect(admin_page.get_by_test_id("spend-total")).to_contain_text("3.75")

    # Group by model only, over the whole range, then drill into the sonnet row.
    admin_page.get_by_test_id("spend-period").select_option("")
    admin_page.get_by_test_id("spend-submit").click()
    expect(rows).to_have_count(2)
    expect(chart).to_have_attribute("data-sp-drawn", "")
    sonnet = rows.filter(has_text="claude-sonnet-4-5")
    sonnet.get_by_test_id("spend-drill").click()
    admin_page.wait_for_url(f"**/projects/{key}/ai-usage/?**")
    expect(admin_page.locator("tr[data-entry-id]")).to_have_count(2)
    console.assert_no_errors()


def test_admin_report_presets_and_csv(admin_page: Page, api_client: httpx.Client, project: dict) -> None:
    _log(api_client, project["key"], "claude-sonnet-4-5", "2026-03-03", "1.00", unique_key("Admin report "))
    console = ConsoleErrorTracker().attach(admin_page)

    admin_page.goto("/admin/")
    admin_page.get_by_test_id("admin-ai-spend-card").click()
    admin_page.wait_for_url("**/admin/ai-spend/**")
    admin_page.locator('[data-preset="last_month"]').click()
    expect(admin_page.locator('[data-preset="last_month"]')).to_have_attribute("aria-current", "true")

    admin_page.goto(f"/admin/ai-spend/?{MARCH}&group=project")
    expect(admin_page.get_by_test_id("spend-table")).to_be_visible()
    expect(admin_page.get_by_test_id("spend-table")).to_contain_text(project["key"])
    href = admin_page.get_by_test_id("spend-csv").get_attribute("href")
    assert href and href.startswith("/api/v1/ai-usage/report/?") and "format=csv" in href
    resp = api_client.get(href)
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/csv")
    assert project["key"] in resp.text
    console.assert_no_errors()
