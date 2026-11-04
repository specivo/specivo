"""E2E: similar issues under the subject of the new issue form.

The debounce, the minimum length, cancelling and the failure handling live in
``issue-form.js``; there is no JavaScript unit runner, so they are tested here.
Requests to the similar-issues endpoint are watched (and, where a test needs
it, answered) with ``page.route``; the one-second pause is driven with
``page.clock`` so the tests do not wait for it. Rate limiting is off for the
E2E server.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor

import asyncpg
import httpx
import pytest
from playwright.sync_api import BrowserContext, Page, Route, expect

from specivo.testing.e2e_base import create_issue, create_project, wait_for_alpine
from tests.e2e.pages.issue_form_page import IssueFormPage

pytestmark = [pytest.mark.e2e]

SIMILAR_URL = re.compile(r"/api/v1/projects/[^/]+/issues/similar/")
SUBJECT = "Login page fails with timeout error"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _db_value(sql: str, *args: object) -> object:
    url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")

    async def _run() -> object:
        conn = await asyncpg.connect(url)
        try:
            return await conn.fetchval(sql, *args)
        finally:
            await conn.close()

    # pytest-playwright runs an event loop of its own; use a fresh one elsewhere.
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, _run()).result()


def _add_member(api: httpx.Client, project_key: str, login: str, role_name: str) -> None:
    user_id = _db_value("SELECT id FROM users WHERE lower(login) = lower($1)", login)
    role_id = _db_value("SELECT id FROM roles WHERE name = $1 AND builtin = 0", role_name)
    resp = api.post(f"/api/v1/projects/{project_key}/members/", json={"user_id": user_id, "role_ids": [role_id]})
    assert resp.status_code in (200, 201), resp.text


def _project_with_issues(api: httpx.Client, *subjects: str) -> tuple[str, list[dict]]:
    project = create_project(api, prefix="SIM")
    issues = [create_issue(api, project["key"], s) for s in subjects]
    return project["key"], issues


class SimilarRequests:
    """Records requests to the similar-issues endpoint; answers them as told."""

    def __init__(self, page: Page) -> None:
        self.queries: list[str] = []
        self.held: list[Route] = []
        self.respond: Callable[[Route], None] | None = None
        self.hold = False
        page.route(SIMILAR_URL, self._handle)

    def _handle(self, route: Route) -> None:
        url = route.request.url
        q = re.search(r"[?&]q=([^&]*)", url)
        from urllib.parse import unquote_plus

        self.queries.append(unquote_plus(q.group(1)) if q else "")
        if self.hold:
            self.held.append(route)
        elif self.respond is not None:
            self.respond(route)
        else:
            route.continue_()


def _fulfill_json(items: list[dict]) -> Callable[[Route], None]:
    def _respond(route: Route) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(items))

    return _respond


def _item(key: str, subject: str, closed: bool = False) -> dict:
    return {
        "key": key,
        "subject": subject,
        "status": {"id": 1, "name": "Closed" if closed else "New"},
        "is_closed": closed,
        "score": 0.5,
    }


def _open_form(page: Page, project_key: str, *, clock: bool = True) -> IssueFormPage:
    if clock:
        page.clock.install()
    form = IssueFormPage(page)
    form.navigate_new(project_key)
    wait_for_alpine(page)
    form.expect_loaded()
    if clock:
        # The installed clock still flows with real time; hold it so that
        # only run_for() moves it.
        page.clock.pause_at(page.evaluate("Date.now()") + 10)
    return form


def _type(page: Page, text: str) -> None:
    page.locator("#subject").fill(text)


def _settle(page: Page, ms: int = 1000) -> None:
    """Move the held clock by *ms*, let a request answer, then run a frame.

    Alpine shows an ``x-show`` element on the next animation frame, which the
    held clock also controls.
    """
    page.clock.run_for(ms)
    page.wait_for_timeout(150)
    page.clock.run_for(50)
    page.wait_for_timeout(50)


@pytest.fixture
def reporter_page(browser, e2e_server, e2e_seed_data, _user_auth) -> Generator[tuple[Page, str], None, None]:  # noqa: F811
    """The regular E2E user's page; the test makes them a member with the role it needs."""
    _, cookies = _user_auth
    context: BrowserContext = browser.new_context(base_url=e2e_server)
    context.add_cookies(cookies)
    page = context.new_page()
    yield page, e2e_seed_data["user_login"]
    context.close()


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


def test_panel_lists_similar_issue_with_link(admin_page: Page, api_client: httpx.Client) -> None:
    key, [issue] = _project_with_issues(api_client, "Login page shows a timeout error")
    _open_form(admin_page, key)

    _type(admin_page, "Timeout when opening the login page")
    _settle(admin_page)

    row = admin_page.locator("[data-testid=similar-issue]")
    expect(row).to_have_count(1)
    expect(row).to_contain_text(issue["key"])
    expect(row).to_contain_text("Login page shows a timeout error")
    expect(row.locator(".sp-similar-issues-status")).not_to_be_empty()
    link = row.locator("a.sp-similar-issues-link")
    expect(link).to_have_attribute("target", "_blank")
    expect(link).to_have_attribute("rel", "noopener")
    expect(link).to_have_attribute("href", f"/issue/{issue['key']}/")


def test_closed_issue_badge_has_text(admin_page: Page, api_client: httpx.Client) -> None:
    key, _ = _project_with_issues(api_client)
    requests = SimilarRequests(admin_page)
    requests.respond = _fulfill_json([_item(f"{key}-1", SUBJECT, closed=True)])
    _open_form(admin_page, key)

    _type(admin_page, SUBJECT)
    _settle(admin_page)

    badge = admin_page.locator(".sp-similar-issues-status-closed")
    expect(badge).to_have_text("Closed")


# ---------------------------------------------------------------------------
# When requests are sent
# ---------------------------------------------------------------------------


def test_waits_one_second_after_typing(admin_page: Page, api_client: httpx.Client) -> None:
    key, _ = _project_with_issues(api_client)
    requests = SimilarRequests(admin_page)
    requests.respond = _fulfill_json([])
    _open_form(admin_page, key)

    _type(admin_page, SUBJECT)
    admin_page.clock.run_for(999)
    admin_page.wait_for_timeout(150)
    assert requests.queries == []
    admin_page.clock.run_for(1)
    admin_page.wait_for_timeout(150)
    assert requests.queries == [SUBJECT]


@pytest.mark.parametrize(
    ("text", "expected"),
    [("Login pag", 0), ("Login page", 1), ("หน้า", 1), ("หน้", 0)],
    ids=["9-chars", "10-chars", "4-thai", "3-thai"],
)
def test_minimum_length(admin_page: Page, api_client: httpx.Client, text: str, expected: int) -> None:
    key, _ = _project_with_issues(api_client)
    requests = SimilarRequests(admin_page)
    requests.respond = _fulfill_json([])
    _open_form(admin_page, key)

    _type(admin_page, text)
    _settle(admin_page, 1500)

    assert len(requests.queries) == expected


def test_trailing_space_sends_no_second_request(admin_page: Page, api_client: httpx.Client) -> None:
    key, _ = _project_with_issues(api_client)
    requests = SimilarRequests(admin_page)
    requests.respond = _fulfill_json([])
    _open_form(admin_page, key)

    _type(admin_page, SUBJECT)
    _settle(admin_page)
    _type(admin_page, SUBJECT + " ")
    _settle(admin_page)
    _type(admin_page, SUBJECT)
    _settle(admin_page)

    assert requests.queries == [SUBJECT]


def test_new_input_cancels_held_request(admin_page: Page, api_client: httpx.Client) -> None:
    key, _ = _project_with_issues(api_client)
    requests = SimilarRequests(admin_page)
    requests.hold = True
    _open_form(admin_page, key)

    _type(admin_page, "First title that never answers")
    _settle(admin_page)
    assert len(requests.held) == 1

    requests.hold = False
    requests.respond = _fulfill_json([_item(f"{key}-2", "Second answer")])
    _type(admin_page, "Second title typed afterwards")
    _settle(admin_page)
    # The first answer arrives late and must not replace the second.
    try:
        requests.held[0].fulfill(
            status=200, content_type="application/json", body=json.dumps([_item(f"{key}-1", "First answer")])
        )
    except Exception:
        pass  # Aborted by the page already.
    admin_page.wait_for_timeout(200)

    rows = admin_page.locator("[data-testid=similar-issue]")
    expect(rows).to_have_count(1)
    expect(rows).to_contain_text("Second answer")


@pytest.mark.parametrize("failure", ["500", "429", "abort"])
def test_failures_leave_an_empty_panel_and_a_working_form(
    admin_page: Page, api_client: httpx.Client, failure: str
) -> None:
    key, _ = _project_with_issues(api_client)
    requests = SimilarRequests(admin_page)

    def _fail(route: Route) -> None:
        if failure == "abort":
            route.abort()
        else:
            route.fulfill(status=int(failure), content_type="application/json", body='{"errors": []}')

    requests.respond = _fail
    form = _open_form(admin_page, key)

    _type(admin_page, SUBJECT)
    _settle(admin_page)

    expect(admin_page.locator("[data-testid=similar-issue]")).to_have_count(0)
    expect(admin_page.locator(".sp-toast, .toast")).to_have_count(0)
    expect(form.subject_input).to_have_value(SUBJECT)
    admin_page.clock.resume()
    admin_page.locator("button", has_text=re.compile(r"^\s*Create Issue\s*$")).first.click()
    admin_page.wait_for_url(re.compile(rf"/issues/{key}-\d+/$"), timeout=10000)
    assert len(requests.queries) == 1


def test_submit_works_while_a_request_never_answers(admin_page: Page, api_client: httpx.Client) -> None:
    key, _ = _project_with_issues(api_client)
    requests = SimilarRequests(admin_page)
    requests.hold = True
    _open_form(admin_page, key)

    _type(admin_page, SUBJECT)
    _settle(admin_page)
    assert len(requests.held) == 1

    admin_page.clock.resume()
    admin_page.locator("button", has_text=re.compile(r"^\s*Create Issue\s*$")).first.click()
    admin_page.wait_for_url(re.compile(rf"/issues/{key}-\d+/$"), timeout=10000)


def test_retries_after_failure_on_next_edit(admin_page: Page, api_client: httpx.Client) -> None:
    key, _ = _project_with_issues(api_client)
    requests = SimilarRequests(admin_page)
    requests.respond = lambda route: route.fulfill(status=500, body="")
    _open_form(admin_page, key)

    _type(admin_page, SUBJECT)
    _settle(admin_page)
    _type(admin_page, SUBJECT + " ")
    _settle(admin_page)

    assert requests.queries == [SUBJECT, SUBJECT]


# ---------------------------------------------------------------------------
# Mark as duplicate
# ---------------------------------------------------------------------------


def test_mark_as_duplicate_creates_relation(admin_page: Page, api_client: httpx.Client) -> None:
    key, [original] = _project_with_issues(api_client, "Login page shows a timeout error")
    _open_form(admin_page, key)

    _type(admin_page, "Timeout when opening the login page")
    _settle(admin_page)
    mark = admin_page.locator(".sp-similar-issues-mark")
    mark.click()
    mark.click()

    pending = admin_page.locator("strong", has_text=re.compile(rf"^{original['key']}$"))
    expect(pending).to_have_count(1)
    expect(pending.locator("xpath=..")).to_contain_text("Duplicates")

    admin_page.clock.resume()
    admin_page.locator("button", has_text=re.compile(r"^\s*Create Issue\s*$")).first.click()
    admin_page.wait_for_url(re.compile(rf"/issues/{key}-\d+/$"), timeout=10000)
    new_key = admin_page.url.rstrip("/").rsplit("/", 1)[-1]

    relations = api_client.get(f"/api/v1/issues/{new_key}/relations/").json()
    assert [(r["relation_type"], r["issue_to_key"]) for r in relations if r["issue_to_key"] == original["key"]] == [
        ("duplicates", original["key"])
    ]


def test_removed_duplicate_creates_no_relation(admin_page: Page, api_client: httpx.Client) -> None:
    key, [original] = _project_with_issues(api_client, "Login page shows a timeout error")
    _open_form(admin_page, key)

    _type(admin_page, "Timeout when opening the login page")
    _settle(admin_page)
    admin_page.locator(".sp-similar-issues-mark").click()
    admin_page.locator("a", has_text="Remove").first.click()

    admin_page.clock.resume()
    admin_page.locator("button", has_text=re.compile(r"^\s*Create Issue\s*$")).first.click()
    admin_page.wait_for_url(re.compile(rf"/issues/{key}-\d+/$"), timeout=10000)
    new_key = admin_page.url.rstrip("/").rsplit("/", 1)[-1]

    assert api_client.get(f"/api/v1/issues/{new_key}/relations/").json() == []


def test_without_relations_permission_row_has_no_button(reporter_page, api_client: httpx.Client) -> None:
    page, login = reporter_page
    key, _ = _project_with_issues(api_client, "Login page shows a timeout error")
    _add_member(api_client, key, login, "Reporter")
    _open_form(page, key)

    _type(page, "Timeout when opening the login page")
    _settle(page)

    expect(page.locator("[data-testid=similar-issue]")).to_have_count(1)
    expect(page.locator(".sp-similar-issues-mark")).to_have_count(0)


# ---------------------------------------------------------------------------
# Where the panel is off
# ---------------------------------------------------------------------------


def test_edit_form_sends_no_requests(admin_page: Page, api_client: httpx.Client) -> None:
    key, [issue] = _project_with_issues(api_client, SUBJECT)
    requests = SimilarRequests(admin_page)
    admin_page.clock.install()
    admin_page.goto(f"/issue/{issue['key']}/edit/")
    wait_for_alpine(admin_page)
    admin_page.clock.pause_at(admin_page.evaluate("Date.now()") + 10)

    _type(admin_page, SUBJECT + " on mobile devices")
    _settle(admin_page, 2000)

    assert requests.queries == []
    expect(admin_page.locator("[data-testid=similar-issues]")).to_have_count(0)


@pytest.mark.parametrize("switch", ["instance", "project"])
def test_switched_off_sends_no_requests(admin_page: Page, api_client: httpx.Client, switch: str) -> None:
    key, _ = _project_with_issues(api_client, SUBJECT)
    if switch == "instance":
        resp = api_client.patch("/api/v1/admin/settings/", json={"similar_issues_enabled": "false"})
    else:
        resp = api_client.patch(f"/api/v1/projects/{key}/", json={"similar_issues": False})
    assert resp.status_code in (200, 204), resp.text
    try:
        requests = SimilarRequests(admin_page)
        _open_form(admin_page, key)

        _type(admin_page, SUBJECT)
        _settle(admin_page, 2000)

        assert requests.queries == []
        expect(admin_page.locator("[data-testid=similar-issues]")).to_have_count(0)
    finally:
        if switch == "instance":
            api_client.patch("/api/v1/admin/settings/", json={"similar_issues_enabled": "true"})


# ---------------------------------------------------------------------------
# Accessibility and reset
# ---------------------------------------------------------------------------


def test_live_region_exists_before_typing_and_focus_stays(admin_page: Page, api_client: httpx.Client) -> None:
    key, _ = _project_with_issues(api_client, "Login page shows a timeout error")
    _open_form(admin_page, key)

    region = admin_page.locator("[data-testid=similar-issues]")
    expect(region).to_have_attribute("aria-live", "polite")

    admin_page.locator("#subject").click()
    admin_page.locator("#subject").press_sequentially("Timeout when opening the login page", delay=5)
    _settle(admin_page)

    expect(admin_page.locator("[data-testid=similar-issue]")).to_have_count(1)
    expect(admin_page.locator("#subject")).to_be_focused()

    # Keyboard: the link and the button are reachable with Tab.
    admin_page.keyboard.press("Tab")
    expect(admin_page.locator("a.sp-similar-issues-link")).to_be_focused()
    admin_page.keyboard.press("Tab")
    expect(admin_page.locator(".sp-similar-issues-mark")).to_be_focused()


def test_create_and_continue_empties_the_panel(admin_page: Page, api_client: httpx.Client) -> None:
    key, _ = _project_with_issues(api_client, "Login page shows a timeout error")
    _open_form(admin_page, key)

    _type(admin_page, "Timeout when opening the login page")
    _settle(admin_page)
    expect(admin_page.locator("[data-testid=similar-issue]")).to_have_count(1)

    admin_page.clock.resume()
    admin_page.locator("button", has_text="Create and Continue").click()
    expect(admin_page.locator("#subject")).to_have_value("")
    expect(admin_page.locator("[data-testid=similar-issue]")).to_have_count(0)


# ---------------------------------------------------------------------------
# Settings pages
# ---------------------------------------------------------------------------


def test_settings_toggles_save_and_show_instance_off_hint(admin_page: Page, api_client: httpx.Client) -> None:
    key, _ = _project_with_issues(api_client)
    try:
        admin_page.goto("/admin/settings/")
        box = admin_page.locator("#similar_issues_enabled")
        expect(box).to_be_checked()
        box.uncheck()
        box.locator("xpath=ancestor::form").locator("button[type=submit]").click()
        admin_page.wait_for_url(re.compile(r"/admin/settings/$"))
        expect(admin_page.locator("#similar_issues_enabled")).not_to_be_checked()

        admin_page.goto(f"/projects/{key}/settings/")
        expect(admin_page.locator("[data-testid=similar-issues-instance-off]")).to_be_visible()
        project_box = admin_page.locator("#project-similar-issues")
        expect(project_box).to_be_checked()
        project_box.uncheck()
        project_box.locator("xpath=ancestor::div[contains(@class,'card')][1]").locator(
            "button", has_text="Save"
        ).click()
        expect(admin_page.locator(".flash", has_text="Saved")).to_be_visible()
        admin_page.reload()
        expect(admin_page.locator("#project-similar-issues")).not_to_be_checked()
    finally:
        api_client.patch("/api/v1/admin/settings/", json={"similar_issues_enabled": "true"})
