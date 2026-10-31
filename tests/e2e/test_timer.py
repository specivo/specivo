"""E2E: the timer from the issue page to the page header and back to a time entry.

Start on the issue page, watch the header tick, stop and log with an activity
(the entry then exists on the issue), or discard (no entry). The timer belongs
to the shared admin account, so every test leaves none running.
"""

from __future__ import annotations

import re
from collections.abc import Generator

import httpx
import pytest
from playwright.sync_api import Page, expect

from specivo.testing.e2e_base import create_issue, create_project, wait_for_alpine
from tests.e2e.e2e_helpers import ConsoleErrorTracker

pytestmark = [pytest.mark.e2e]


def _discard(api: httpx.Client) -> None:
    resp = api.post("/api/v1/timer/discard/")
    assert resp.status_code in (204, 404), resp.text


@pytest.fixture
def no_timer(api_client: httpx.Client) -> Generator[None, None, None]:
    _discard(api_client)
    yield
    _discard(api_client)


@pytest.fixture
def project(api_client: httpx.Client) -> dict:
    return create_project(api_client, prefix="TMR")


@pytest.fixture
def issue(api_client: httpx.Client, project: dict) -> dict:
    return create_issue(api_client, project["key"], "Measure the boiler pressure")


def _entries(api: httpx.Client, project_key: str) -> list[dict]:
    resp = api.get(f"/api/v1/projects/{project_key}/time-entries/")
    assert resp.status_code == 200, resp.text
    return resp.json()["items"]


def _start_on_issue_page(page: Page, issue: dict) -> None:
    page.goto(f"/issue/{issue['key']}/")
    wait_for_alpine(page)
    page.get_by_role("button", name="Start timer").click()
    page.wait_for_load_state("load")
    expect(page.locator(".sp-timer-start-running")).to_be_visible()


def test_start_tick_stop_and_log(
    admin_page: Page, api_client: httpx.Client, project: dict, issue: dict, no_timer: None
) -> None:
    tracker = ConsoleErrorTracker().attach(admin_page)
    _start_on_issue_page(admin_page, issue)

    header = admin_page.locator(".sp-header-timer")
    expect(header.locator(".header-timer")).to_contain_text(issue["key"])
    elapsed = header.locator(".timer-elapsed")
    first = elapsed.inner_text()
    expect(elapsed).not_to_have_text(first, timeout=5000)

    header.locator(".header-timer").click()
    expect(header.get_by_text("Measure the boiler pressure")).to_be_visible()
    header.get_by_role("button", name="Stop & log").click()
    select = header.locator("#sp-timer-activity")
    expect(select).to_have_value(_activity_id(api_client, "Development"))
    select.select_option(label="Testing")
    header.locator("#sp-timer-comment").fill("Pressure within limits")
    header.get_by_role("button", name="Log time").click()

    expect(admin_page.locator(".sp-header-timer")).to_have_count(0)
    [entry] = _entries(api_client, project["key"])
    assert entry["issue_id"] == issue["id"]
    assert entry["activity"]["name"] == "Testing"
    assert entry["comments"] == "Pressure within limits"
    tracker.assert_no_errors()


def test_discard_leaves_no_entry(
    admin_page: Page, api_client: httpx.Client, project: dict, issue: dict, no_timer: None
) -> None:
    _start_on_issue_page(admin_page, issue)

    header = admin_page.locator(".sp-header-timer")
    header.locator(".header-timer").click()
    header.get_by_role("button", name="Discard").first.click()
    header.locator(".sp-timer-discard-confirm").click()

    expect(admin_page.locator(".sp-header-timer")).to_have_count(0)
    assert api_client.get("/api/v1/timer/").json() is None
    assert _entries(api_client, project["key"]) == []


def test_starting_with_a_timer_elsewhere_asks_first(
    admin_page: Page, api_client: httpx.Client, project: dict, issue: dict, no_timer: None
) -> None:
    other = create_issue(api_client, project["key"], "Bleed the radiators")
    started = api_client.post("/api/v1/timer/start/", json={"project_id": project["id"], "issue_id": other["id"]})
    assert started.status_code == 201, started.text

    admin_page.goto(f"/issue/{issue['key']}/")
    wait_for_alpine(admin_page)
    admin_page.get_by_role("button", name="Start timer").click()
    confirm = admin_page.locator(".sp-timer-start-confirm")
    expect(confirm).to_contain_text(other["key"])
    confirm.get_by_role("button", name="Discard it and start").click()

    expect(admin_page.locator(".sp-timer-start-running")).to_be_visible()
    assert api_client.get("/api/v1/timer/").json()["issue_id"] == issue["id"]
    assert _entries(api_client, project["key"]) == []


def test_log_it_and_start_says_what_happened_to_the_running_timer(
    admin_page: Page, api_client: httpx.Client, project: dict, issue: dict, no_timer: None
) -> None:
    other = create_issue(api_client, project["key"], "Drain the expansion tank")
    started = api_client.post("/api/v1/timer/start/", json={"project_id": project["id"], "issue_id": other["id"]})
    assert started.status_code == 201, started.text

    admin_page.goto(f"/issue/{issue['key']}/")
    wait_for_alpine(admin_page)
    admin_page.get_by_role("button", name="Start timer").click()
    admin_page.locator(".sp-timer-start-confirm").get_by_role("button", name="Log it and start").click()

    expect(admin_page.locator(".sp-timer-start-running")).to_be_visible()
    notice = admin_page.get_by_role("status").filter(has_text="was stopped")
    expect(notice).to_contain_text(f"The timer on {other['key']} was stopped and")
    expect(notice).to_contain_text("logged with the default activity")
    [entry] = _entries(api_client, project["key"])
    assert entry["issue_id"] == other["id"]

    # Shown once: the next visit has no notice.
    notice.get_by_role("button", name="Dismiss").click()
    expect(notice).to_be_hidden()
    admin_page.reload()
    wait_for_alpine(admin_page)
    expect(admin_page.locator(".sp-timer-notice")).to_be_hidden()


def test_header_timer_name_includes_the_elapsed_time(
    admin_page: Page, api_client: httpx.Client, project: dict, issue: dict, no_timer: None
) -> None:
    _start_on_issue_page(admin_page, issue)

    button = admin_page.get_by_role(
        "button", name=re.compile(rf"^Timer running on {re.escape(issue['key'])} \d+:\d\d:\d\d$")
    )
    expect(button).to_be_visible()
    elapsed = admin_page.locator(".sp-header-timer .timer-elapsed").inner_text()
    expect(button).not_to_have_accessible_name(re.compile(rf"{re.escape(elapsed)}$"), timeout=5000)


def _activity_id(api: httpx.Client, name: str) -> str:
    resp = api.get("/api/v1/time-entries/activities/")
    assert resp.status_code == 200, resp.text
    return str(next(a["id"] for a in resp.json() if a["name"] == name))
