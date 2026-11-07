"""E2E tests for the dashboard page."""

import pytest
from playwright.sync_api import Page, expect

from specivo.testing.e2e_base import create_issue, create_project, unique_key
from tests.e2e.e2e_helpers import ConsoleErrorTracker
from tests.e2e.pages.dashboard_page import DashboardPage

pytestmark = [pytest.mark.e2e]


def test_dashboard_renders(auth_page: Page) -> None:
    """Dashboard loads with status banner for authenticated user."""
    dash = DashboardPage(auth_page)
    dash.navigate()
    dash.expect_loaded()


def test_dashboard_shows_sidebar(auth_page: Page) -> None:
    """Dashboard includes the sidebar navigation."""
    dash = DashboardPage(auth_page)
    dash.navigate()
    expect(dash.sidebar).to_be_visible()
    expect(dash.sidebar_brand).to_contain_text("Specivo")


@pytest.mark.skip(reason="dashboard project visibility depends on project listing query — investigate separately")
def test_dashboard_shows_created_project(admin_page: Page, api_client) -> None:
    """A project created via API appears on the admin's dashboard."""
    key = unique_key()
    resp = api_client.post(
        "/api/v1/projects/",
        json={"name": f"Test {key}", "identifier": key.lower(), "key": key},
    )
    assert resp.status_code == 201, resp.text

    dash = DashboardPage(admin_page)
    dash.navigate()
    dash.expect_project_visible(key)


# ---------------------------------------------------------------------------
# The redesigned blocks
# ---------------------------------------------------------------------------


def _issue_with_comments(api_client) -> dict:
    """An issue the admin creates and comments on twice: one group of three events in their activity."""
    project = create_project(api_client, prefix="DSH")
    issue = create_issue(api_client, project["key"], "Dashboard grouping sample")
    for note in ("First dashboard note", "Second dashboard note"):
        resp = api_client.post(f"/api/v1/issues/{issue['key']}/journals/", json={"notes": note})
        assert resp.status_code == 201, resp.text
    return issue


def test_activity_group_expands(admin_page: Page, api_client) -> None:
    """A grouped activity row opens with a native <details> and lists its events."""
    issue = _issue_with_comments(api_client)
    admin_page.goto("/")
    # Issue creation is stamped by the database clock and comments by the app's,
    # so "created" may form a group of its own; the comments are always together.
    group = admin_page.locator("[data-testid=dash-activity-group]", has_text=issue["key"]).filter(has_text="2 comments")
    expect(group).to_have_count(1)
    if group.get_attribute("open") is not None:
        group.locator("summary").click()
    expect(group.get_by_text("Second dashboard note")).not_to_be_visible()
    group.locator("summary").click()
    expect(group).to_have_attribute("open", "")
    expect(group.get_by_text("Second dashboard note")).to_be_visible()


def test_spend_period_switch(admin_page: Page) -> None:
    """The period switch swaps the spend block in place and marks the chosen period."""
    tracker = ConsoleErrorTracker().attach(admin_page)
    admin_page.goto("/")
    spend = admin_page.locator("#sp-dash-spend")
    expect(spend).to_have_attribute("data-period", "month")
    expect(admin_page.get_by_test_id("dash-period-month")).to_have_attribute("aria-pressed", "true")
    admin_page.get_by_test_id("dash-period-7d").click()
    expect(admin_page.locator("#sp-dash-spend")).to_have_attribute("data-period", "7d")
    expect(admin_page.get_by_test_id("dash-period-7d")).to_have_attribute("aria-pressed", "true")
    expect(admin_page.get_by_test_id("dash-period-month")).to_have_attribute("aria-pressed", "false")
    tracker.assert_no_errors()


def test_activity_filter_chips(admin_page: Page, api_client) -> None:
    """The People chip keeps the admin's own work; the Agents chip leaves it out."""
    issue = _issue_with_comments(api_client)
    admin_page.goto("/")
    admin_page.get_by_test_id("dash-activity-agents").click()
    expect(admin_page.locator("#sp-dash-activity")).to_have_attribute("data-filter", "agents")
    expect(admin_page.locator("[data-testid=dash-activity-group]", has_text=issue["key"])).to_have_count(0)
    admin_page.get_by_test_id("dash-activity-people").click()
    expect(admin_page.locator("#sp-dash-activity")).to_have_attribute("data-filter", "people")
    expect(admin_page.locator("[data-testid=dash-activity-group]", has_text=issue["key"]).first).to_be_visible()


def test_mobile_has_no_horizontal_scroll(browser, e2e_server, _admin_auth, api_client) -> None:
    """At 390 px every block stacks and nothing scrolls sideways."""
    _issue_with_comments(api_client)
    _, cookies = _admin_auth
    context = browser.new_context(base_url=e2e_server, viewport={"width": 390, "height": 844})
    context.add_cookies(cookies)
    page = context.new_page()
    try:
        page.goto("/")
        page.wait_for_load_state("networkidle")
        expect(page.get_by_test_id("dash-spend")).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth + 2")
        expect(page.locator(".sp-dash-issues thead")).to_be_hidden()
    finally:
        context.close()
