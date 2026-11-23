"""E2E: AI billing accounts in the browser, and the project's AI usage payer default.

The shared admin account adds a company account with a unique slug and sets
the payer default of a fresh project, so the assertions look only at what the
test wrote.
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import Page, expect

from specivo.testing.e2e_base import create_project, unique_key
from tests.e2e.e2e_helpers import ConsoleErrorTracker

pytestmark = [pytest.mark.e2e]


def test_admin_adds_and_switches_off_a_company_account(admin_page: Page, api_client: httpx.Client) -> None:
    slug = unique_key("gw-").lower()
    console = ConsoleErrorTracker().attach(admin_page)

    admin_page.goto("/admin/ai-billing-accounts/")
    form = admin_page.get_by_test_id("billing-account-form")
    form.get_by_test_id("billing-account-name").fill("E2E gateway")
    form.get_by_test_id("billing-account-slug").fill(slug)
    form.get_by_test_id("billing-account-kind").select_option("gateway")
    form.get_by_test_id("billing-account-payer").select_option("employer")
    form.get_by_test_id("billing-account-routes").fill(f"{slug}-*")
    form.get_by_test_id("billing-account-submit").click()

    expect(admin_page.get_by_test_id("billing-account-notice")).to_be_visible()
    row = admin_page.locator("tr", has_text=slug)
    expect(row).to_have_count(1)
    row.get_by_role("link").click()
    admin_page.get_by_test_id("billing-account-deactivate").click()
    expect(admin_page.get_by_test_id("billing-account-notice")).to_be_visible()

    accounts = api_client.get("/api/v1/admin/ai-billing-accounts/?limit=200").json()["items"]
    [mine] = [a for a in accounts if a["slug"] == slug]
    assert mine["is_active"] is False and mine["route_patterns"] == [f"{slug}-*"]
    console.assert_no_errors()


def test_project_settings_save_the_ai_payer_default(admin_page: Page, api_client: httpx.Client) -> None:
    project = create_project(api_client, prefix="BPD")
    console = ConsoleErrorTracker().attach(admin_page)

    admin_page.goto(f"/projects/{project['key']}/settings/")
    select = admin_page.get_by_test_id("project-ai-payer-default")
    expect(select).to_have_value("none")
    select.select_option("employer")
    admin_page.get_by_test_id("project-general-save").click()
    expect(admin_page.get_by_test_id("project-general-message")).to_be_visible()

    resp = api_client.get(f"/api/v1/projects/{project['key']}/")
    assert resp.json()["ai_usage_payer_default"] == "employer"
    admin_page.reload()
    expect(admin_page.get_by_test_id("project-ai-payer-default")).to_have_value("employer")
    console.assert_no_errors()
