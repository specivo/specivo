"""The whole UI behind ``STEALTH_PREFIX``, in a browser.

A second server runs with ``STEALTH_PREFIX=/hidden-ui`` (``e2e_prefixed_server``).
The smoke tests walk the main flows; the crawler signs in as an admin, collects
every same-origin link of the main pages and follows them. Every link must stay
under the prefix and none may answer 404, and no asset or API request a page
makes may fail.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx
import pytest
from playwright.sync_api import Page, expect

from specivo.testing.e2e_base import (
    E2E_PASSWORD,
    E2E_PREFIXED_ORIGIN,
    E2E_STEALTH_PREFIX,
    api_post_with_retry,
    create_issue,
    create_project,
    unique_key,
    wait_for_alpine,
)

pytestmark = [pytest.mark.e2e]

MAX_CRAWLED = 150


@dataclass
class _Problems:
    """What went wrong on the pages a test opened: console errors and failed requests."""

    console: list[str] = field(default_factory=list)
    requests: list[str] = field(default_factory=list)

    def assert_none(self) -> None:
        assert self.requests == [], self.requests
        assert self.console == [], self.console


def _watch(page: Page) -> _Problems:
    problems = _Problems()

    def on_console(msg) -> None:
        if msg.type == "error":
            problems.console.append(f"{page.url}: {msg.text}")

    def on_response(response) -> None:
        if response.url.startswith(E2E_PREFIXED_ORIGIN) and response.status >= 400:
            problems.requests.append(f"{response.status} {response.url} (on {page.url})")

    def on_failed(request) -> None:
        if request.url.startswith(E2E_PREFIXED_ORIGIN):
            problems.requests.append(f"failed {request.url}: {request.failure} (on {page.url})")

    page.on("console", on_console)
    page.on("response", on_response)
    page.on("requestfailed", on_failed)
    return problems


@pytest.fixture
def world(prefixed_api: httpx.Client) -> Iterator[dict]:
    """A project with the wiki on, two issues (one linking the other) and a wiki page."""
    project = create_project(prefixed_api, prefix="HID")
    key = project["key"]
    prefixed_api.patch(f"/api/v1/projects/{key}/modules/", json={"modules": {"wiki": True}})
    first = create_issue(prefixed_api, key, f"Hidden first {unique_key()}")
    second = create_issue(prefixed_api, key, f"Hidden second {unique_key()}", description=f"Depends on {first['key']}.")
    page = api_post_with_retry(
        prefixed_api,
        f"/api/v1/projects/{key}/wiki/",
        json={"title": "Hidden Notes", "text": f"See {second['key']} and [[Home]]."},
    )
    assert page.status_code == 201, page.text
    yield {"key": key, "first": first["key"], "second": second["key"], "slug": page.json()["slug"]}


def _under_prefix(url: str, base: str) -> bool:
    return url == base or url.startswith((base + "/", base + "?"))


# ---------------------------------------------------------------------------
# Smoke
# ---------------------------------------------------------------------------


def test_login_lands_under_the_prefix(page: Page, e2e_prefixed_server: str, e2e_seed_data: dict) -> None:
    base = e2e_prefixed_server
    problems = _watch(page)
    page.goto(f"{base}/projects/")
    # Refused: sent to the login page under the prefix, with the way back.
    page.wait_for_url(re.compile(re.escape(base) + r"/login/\?next="))
    page.locator("#login-field").fill(e2e_seed_data["admin_login"])
    page.locator("#password-field").fill(E2E_PASSWORD)
    page.locator("button.sp-btn-login").click()
    page.wait_for_url(f"{base}/projects/", timeout=10000)
    expect(page.locator("h1").first).to_be_visible()
    problems.assert_none()


def test_main_flows_under_the_prefix(prefixed_admin_page: Page, e2e_prefixed_server: str, world: dict) -> None:
    base = e2e_prefixed_server
    page = prefixed_admin_page
    problems = _watch(page)
    key = world["key"]

    page.goto(f"{base}/")
    wait_for_alpine(page)

    page.goto(f"{base}/projects/")
    page.locator(f"a[href='{E2E_STEALTH_PREFIX}/projects/{key}/']").first.click()
    page.wait_for_url(f"{base}/projects/{key}/")

    page.goto(f"{base}/projects/{key}/issues/")
    page.locator(f"a[href='{E2E_STEALTH_PREFIX}/issue/{world['second']}/']").first.click()
    page.wait_for_url(f"{base}/issue/{world['second']}/")
    # The Markdown issue reference carries the prefix and leads to the issue.
    page.locator(f"a[href='{E2E_STEALTH_PREFIX}/issue/{world['first']}/']", has_text=world["first"]).first.click()
    page.wait_for_url(f"{base}/issue/{world['first']}/")

    # Create an issue through the form: the save and the redirect stay under the prefix.
    page.goto(f"{base}/projects/{key}/issues/new/")
    wait_for_alpine(page)
    subject = f"Hidden created {unique_key()}"
    page.locator("#subject").click()
    page.locator("#subject").press_sequentially(subject, delay=10)
    submit = page.locator("button.sp-btn-primary").first
    expect(submit).to_be_enabled(timeout=5000)
    submit.click()
    page.wait_for_url(re.compile(re.escape(f"{base}/issue/{key}-") + r"\d+/$"), timeout=10000)
    expect(page.locator("h1.issue-title", has_text=subject).first).to_be_visible()

    page.goto(f"{base}/projects/{key}/wiki/{world['slug']}/")
    issue_ref = page.locator(f"a[href='{E2E_STEALTH_PREFIX}/issue/{world['second']}/']", has_text=world["second"])
    expect(issue_ref.first).to_be_visible()

    page.goto(f"{base}/search/?q=Hidden")
    expect(page.locator(f"a[href^='{E2E_STEALTH_PREFIX}/issue/{key}-']").first).to_be_visible(timeout=10000)

    page.goto(f"{base}/projects/{key}/settings/")
    wait_for_alpine(page)
    expect(page.locator("h1").first).to_be_visible()

    problems.assert_none()


def test_nothing_is_served_outside_the_prefix(e2e_prefixed_server: str) -> None:
    for path in ("/", "/login/", "/projects/", "/static/img/favicon.svg", "/api/v1/projects/"):
        resp = httpx.get(f"{E2E_PREFIXED_ORIGIN}{path}", headers={"Accept": "text/html"}, timeout=10)
        assert resp.status_code == 404, path
        assert E2E_STEALTH_PREFIX not in resp.text
    assert httpx.get(f"{e2e_prefixed_server}/static/img/favicon.svg", timeout=10).status_code == 200


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------


def _same_origin_links(page: Page) -> list[str]:
    hrefs = page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
    return [h.split("#", 1)[0] for h in hrefs if h.startswith(E2E_PREFIXED_ORIGIN)]


def test_crawl_stays_under_the_prefix(prefixed_admin_page: Page, e2e_prefixed_server: str, world: dict) -> None:
    base = e2e_prefixed_server
    page = prefixed_admin_page
    problems = _watch(page)
    key = world["key"]
    start = [
        "/",
        "/projects/",
        f"/projects/{key}/",
        f"/projects/{key}/issues/",
        f"/issue/{world['second']}/",
        f"/projects/{key}/wiki/{world['slug']}/",
        "/search/?q=Hidden",
        f"/projects/{key}/settings/",
        "/admin/",
        "/my/profile/",
    ]

    found: dict[str, str] = {}
    for path in start:
        page.goto(f"{base}{path}")
        page.wait_for_load_state("networkidle")
        for link in _same_origin_links(page):
            found.setdefault(link, page.url)

    outside = sorted(f"{link} (on {source})" for link, source in found.items() if not _under_prefix(link, base))
    assert outside == [], outside

    # Sign-out links would end the session the crawl runs in.
    links = [link for link in sorted(found) if "/logout/" not in urlsplit(link).path][:MAX_CRAWLED]
    assert len(links) > 20, links
    missing = []
    for link in links:
        resp = page.request.get(link)
        if resp.status == 404:
            missing.append(f"{link} (on {found[link]})")
    assert missing == [], missing

    # A sample of the linked pages, opened in the browser: their own assets and
    # API calls must load too.
    for link in links[:: max(1, len(links) // 25)]:
        page.goto(link)
        page.wait_for_load_state("networkidle")
    problems.assert_none()
