"""The original and remaining estimates are edited in the issue sidebar and the edit form."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
from playwright.sync_api import Page, expect

from specivo.testing.e2e_base import create_issue, create_project, unique_key

pytestmark = [pytest.mark.e2e]


def test_sidebar_saves_the_remaining_estimate(admin_page: Page, api_client: httpx.Client) -> None:
    project = create_project(api_client, prefix="EST")
    issue = create_issue(api_client, project["key"], f"Sidebar estimate {unique_key()}", original_estimate=8)

    admin_page.goto(f"/issue/{issue['key']}/")
    admin_page.wait_for_load_state("networkidle")
    admin_page.locator("input[x-ref='remH']").fill("2")
    admin_page.locator("input[x-ref='remM']").fill("15")
    with admin_page.expect_navigation():
        admin_page.locator("button[data-field='remaining_estimate']").click()

    expect(admin_page.locator("input[x-ref='remH']")).to_have_value("2")
    expect(admin_page.locator("input[x-ref='remM']")).to_have_value("15")
    expect(admin_page.locator("input[x-ref='origH']")).to_have_value("8")
    data = api_client.get(f"/api/v1/issues/{issue['key']}/").json()
    assert Decimal(data["remaining_estimate"]) == Decimal("2.25")
    assert Decimal(data["original_estimate"]) == 8
    assert data["estimated_hours"] is None


def test_sidebar_saves_a_zero_remaining_estimate(admin_page: Page, api_client: httpx.Client) -> None:
    project = create_project(api_client, prefix="EST")
    issue = create_issue(api_client, project["key"], f"Zero left {unique_key()}", remaining_estimate=3)

    admin_page.goto(f"/issue/{issue['key']}/")
    admin_page.wait_for_load_state("networkidle")
    admin_page.locator("input[x-ref='remH']").fill("0")
    admin_page.locator("input[x-ref='remM']").fill("")
    with admin_page.expect_navigation():
        admin_page.locator("button[data-field='remaining_estimate']").click()

    assert Decimal(api_client.get(f"/api/v1/issues/{issue['key']}/").json()["remaining_estimate"]) == 0


def test_edit_form_saves_both_estimates(admin_page: Page, api_client: httpx.Client) -> None:
    project = create_project(api_client, prefix="EST")
    issue = create_issue(api_client, project["key"], f"Form estimate {unique_key()}", remaining_estimate=1.5)

    admin_page.goto(f"/issue/{issue['key']}/edit/")
    admin_page.wait_for_load_state("networkidle")
    expect(admin_page.locator("input[x-model='rem_hours']")).to_have_value("1")
    expect(admin_page.locator("input[x-model='rem_minutes']")).to_have_value("30")
    admin_page.locator("input[x-model='orig_hours']").fill("10")
    admin_page.locator("input[x-model='rem_hours']").fill("4")
    admin_page.locator("input[x-model='rem_minutes']").fill("0")
    with admin_page.expect_navigation():
        admin_page.locator(".form-actions .sp-btn-primary").click()

    data = api_client.get(f"/api/v1/issues/{issue['key']}/").json()
    assert Decimal(data["original_estimate"]) == 10
    assert Decimal(data["remaining_estimate"]) == 4
