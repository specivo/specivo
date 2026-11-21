"""E2E: AI spend by release and sprint: the report's release level, the version page card, the sprint page.

A fresh project per test with one release and one sprint set on a parent
issue; the usage is logged on its sub-issue, which inherits both, and on an
unplanned issue.
"""

from __future__ import annotations

import re

import httpx
import pytest
from playwright.sync_api import Page, expect

from specivo.testing.e2e_base import create_issue, create_project
from tests.e2e.e2e_helpers import ConsoleErrorTracker

pytestmark = [pytest.mark.e2e]

MARCH = "from_date=2026-03-01&to_date=2026-03-31"


def _log(api: httpx.Client, project_key: str, issue_id: int | None, amount: str) -> None:
    resp = api.post(
        f"/api/v1/projects/{project_key}/ai-usage/",
        json={
            "tool": "claude-code",
            "model": "claude-sonnet-4-5",
            "usage_date": "2026-03-12",
            "tokens": {"input": 100_000, "output": 5_000},
            "cost_type": "api",
            "cost": {"amount": amount, "currency": "USD"},
            "issue_id": issue_id,
        },
    )
    assert resp.status_code == 201, resp.text


@pytest.fixture
def planned(api_client: httpx.Client) -> dict:
    """A project with release 1.0 and Sprint 1 on a feature, usage on its task (inherited) and elsewhere."""
    project = create_project(api_client, prefix="ASP")
    key = project["key"]
    version = api_client.post(f"/api/v1/projects/{key}/versions/", json={"name": "1.0"})
    assert version.status_code == 201, version.text
    sprint = api_client.post(f"/api/v1/projects/{key}/sprints/", json={"name": "Sprint 1"})
    assert sprint.status_code == 201, sprint.text
    feature = create_issue(
        api_client, key, "Planned feature", fixed_version_id=version.json()["id"], sprint_id=sprint.json()["id"]
    )
    task = create_issue(api_client, key, "Task without a release", parent_id=feature["id"], estimated_hours=3)
    other = create_issue(api_client, key, "Unplanned work")
    _log(api_client, key, task["id"], "2.50")
    _log(api_client, key, other["id"], "1.00")
    return {"key": key, "version_id": version.json()["id"], "sprint_id": sprint.json()["id"]}


def test_report_release_level_and_drill_down(admin_page: Page, planned: dict) -> None:
    key = planned["key"]
    console = ConsoleErrorTracker().attach(admin_page)
    admin_page.goto(f"/projects/{key}/ai-usage/report/?{MARCH}&group=user")
    tabs = admin_page.get_by_test_id("spend-tabs")
    tabs.locator('[data-group="version"]').click()
    admin_page.wait_for_url("**group=version**")
    rows = admin_page.get_by_test_id("spend-row")
    expect(rows).to_have_count(2)
    release = admin_page.get_by_test_id("spend-open").filter(has_text=re.compile(r"^\s*1\.0\s*$"))
    expect(rows.filter(has=release)).to_contain_text("2.50")
    expect(rows.filter(has_text="No release")).to_contain_text("1.00")

    # Opening the release narrows the report to it: the path names it, the total is its spend.
    release.click()
    admin_page.wait_for_url(f"**version={planned['version_id']}**")
    expect(admin_page.get_by_test_id("spend-crumbs")).to_contain_text("Release")
    expect(admin_page.get_by_test_id("spend-crumbs")).to_contain_text("1.0")
    expect(admin_page.get_by_test_id("spend-total")).to_contain_text("2.50")
    expect(tabs.locator('[data-group="version"]')).to_have_attribute("aria-disabled", "true")

    # The sprint grouping counts the sub-issue under the parent's sprint.
    admin_page.goto(f"/projects/{key}/ai-usage/report/?{MARCH}&group=sprint")
    sprint = admin_page.get_by_test_id("spend-open").filter(has_text=re.compile(r"^\s*Sprint 1\s*$"))
    expect(rows.filter(has=sprint)).to_contain_text("2.50")
    expect(rows.filter(has_text="No sprint")).to_contain_text("1.00")
    console.assert_no_errors()


def test_version_page_card_links_the_report(admin_page: Page, planned: dict) -> None:
    key = planned["key"]
    admin_page.goto(f"/projects/{key}/versions/{planned['version_id']}/")
    card = admin_page.get_by_test_id("ai-spend-card")
    expect(card).to_be_visible()
    expect(card.get_by_test_id("ai-spend-card-spend")).to_contain_text("2.50")
    expect(card.get_by_test_id("ai-spend-card-people")).to_have_text("1")
    card.get_by_test_id("ai-spend-card-link").click()
    admin_page.wait_for_url(f"**/projects/{key}/ai-usage/report/**version={planned['version_id']}**")
    expect(admin_page.get_by_test_id("spend-total")).to_contain_text("2.50")


def test_sprint_page_time_and_card(admin_page: Page, planned: dict) -> None:
    key = planned["key"]
    admin_page.goto(f"/projects/{key}/sprints/{planned['sprint_id']}/")
    expect(admin_page.get_by_test_id("sprint-time-estimated")).to_have_text("3.0h")
    expect(admin_page.get_by_test_id("sprint-time-logged")).to_have_text("0.0h")
    card = admin_page.get_by_test_id("ai-spend-card")
    expect(card.get_by_test_id("ai-spend-card-spend")).to_contain_text("2.50")
    # The two cards sit side by side on a desktop and stack on a phone.
    plan = admin_page.get_by_test_id("sprint-plan")
    time_box = admin_page.get_by_test_id("sprint-time").bounding_box()
    card_box = card.bounding_box()
    assert time_box is not None and card_box is not None
    assert abs(time_box["y"] - card_box["y"]) < 2
    admin_page.set_viewport_size({"width": 390, "height": 844})
    expect(plan).to_be_visible()
    time_box = admin_page.get_by_test_id("sprint-time").bounding_box()
    card_box = card.bounding_box()
    assert time_box is not None and card_box is not None
    assert card_box["y"] > time_box["y"] + time_box["height"] - 1
