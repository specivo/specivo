"""E2E: the Development tab on the issue page shows linked pull requests and commits in its timeline.

No Celery worker runs in the E2E environment, so the commits and the pull
request are written to the database directly, as processing would store them.
"""

import os
import re
import subprocess

import httpx
import pytest
from playwright.sync_api import Page, expect

from specivo.testing.e2e_base import create_issue, create_project
from tests.e2e.e2e_helpers import ConsoleErrorTracker

pytestmark = [pytest.mark.e2e]

_REPO = "example-org/widgets"
_SEED = """
import asyncio, asyncpg, sys
async def _seed(issue_id, integration_id):
    conn = await asyncpg.connect(sys.argv[1])
    try:
        pr_id = await conn.fetchval(
            "INSERT INTO pull_requests (integration_id, provider, number, title, url, state, source_branch,"
            " target_branch, author_login) VALUES ($1, 'github', 7, 'Add the login form', $2, 'open',"
            " 'feat/login-form', 'main', 'alex-example') RETURNING id",
            integration_id, "https://github.example.com/example-org/widgets/pull/7",
        )
        await conn.execute(
            "INSERT INTO issue_pull_request_links (pull_request_id, issue_id, source, closing)"
            " VALUES ($1, $2, 'body', true)", pr_id, issue_id,
        )
        for n in range(7):
            sha = format(n, "x") * 40
            commit_id = await conn.fetchval(
                "INSERT INTO repository_commits (integration_id, sha, message, url, author_name, author_email,"
                " committed_at, branch) VALUES ($1, $2, $3, $4, 'Alex Example', 'alex@example.com',"
                " now() - make_interval(hours => $5), 'feat/login-form') RETURNING id",
                integration_id, sha, f"Step {n} of the login form",
                f"https://github.example.com/{sys.argv[4]}/commit/{sha}", n,
            )
            await conn.execute(
                "INSERT INTO issue_commit_links (commit_id, issue_id, source, keyword) VALUES ($1, $2, 'branch', NULL)",
                commit_id, issue_id,
            )
    finally:
        await conn.close()
asyncio.run(_seed(int(sys.argv[2]), int(sys.argv[3])))
"""


def _seed(issue_id: int, integration_id: int) -> None:
    db_url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    result = subprocess.run(
        ["uv", "run", "python", "-c", _SEED, db_url, str(issue_id), str(integration_id), _REPO],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.fixture
def issue_with_development(api_client: httpx.Client) -> dict:
    project = create_project(api_client)
    issue = create_issue(api_client, project["key"], "Wire the login form")
    resp = api_client.post(
        f"/api/v1/projects/{project['key']}/repositories/",
        json={"provider": "github", "repo_full_name": _REPO, "repo_url": f"https://github.example.com/{_REPO}"},
    )
    assert resp.status_code == 201, resp.text
    _seed(issue["id"], resp.json()["id"])
    return issue


def _tab(page: Page):
    return page.locator(".activity-tabs-header").get_by_test_id("development-tab")


def test_no_tab_without_linked_work(admin_page: Page, api_client: httpx.Client) -> None:
    project = create_project(api_client)
    issue = create_issue(api_client, project["key"], "Nothing linked yet")
    admin_page.goto(f"/issue/{issue['key']}/")
    expect(admin_page.locator(".activity-tabs-header")).to_be_visible()
    expect(_tab(admin_page)).to_have_count(0)
    expect(admin_page.get_by_test_id("development-panel")).to_have_count(0)


def test_tab_shows_linked_work(admin_page: Page, issue_with_development: dict) -> None:
    tracker = ConsoleErrorTracker().attach(admin_page)
    admin_page.goto(f"/issue/{issue_with_development['key']}/")
    tabs = admin_page.locator(".activity-tabs-header [data-tab]")
    expect(tabs.first).to_have_attribute("data-tab", "activity")
    expect(tabs.nth(1)).to_have_attribute("data-tab", "development")
    # Activity stays the default; the panel waits behind its tab.
    expect(tabs.first).to_have_class(re.compile(r"\bactive\b"))
    panel = admin_page.get_by_test_id("development-panel")
    expect(panel).to_be_hidden()

    tab = _tab(admin_page)
    expect(tab.locator(".tab-count")).to_have_text("8")  # one pull request and seven commits
    tab.click()
    expect(panel).to_be_visible()
    expect(admin_page).to_have_url(re.compile(r"#development$"))

    pr_link = panel.get_by_role("link", name="#7")
    expect(pr_link).to_have_attribute("href", f"https://github.example.com/{_REPO}/pull/7")
    expect(pr_link).to_have_attribute("target", "_blank")
    expect(panel).to_contain_text("Add the login form")
    expect(panel).to_contain_text("Open")
    expect(panel).not_to_contain_text("Branches")
    # The commit's branch, in its meta line, links to the forge.
    expect(panel.get_by_role("link", name="feat/login-form").first).to_have_attribute(
        "href", f"https://github.example.com/{_REPO}/tree/feat/login-form"
    )

    # Commits are timeline entries now: all seven fit on the first page, newest first.
    expect(panel.get_by_text("Step 0 of the login form")).to_be_visible()
    expect(panel.get_by_text("Step 6 of the login form")).to_be_visible()
    expect(panel.get_by_test_id("development-more")).to_have_count(0)
    expect(panel).not_to_contain_text("alex@example.com")
    tracker.assert_no_errors()


def test_hash_opens_the_tab(admin_page: Page, issue_with_development: dict) -> None:
    admin_page.goto(f"/issue/{issue_with_development['key']}/#development")
    expect(admin_page.get_by_test_id("development-panel")).to_be_visible()
    expect(_tab(admin_page)).to_have_class(re.compile(r"\bactive\b"))
    # Back to Activity drops the hash.
    admin_page.locator('.activity-tabs-header [data-tab="activity"]').click()
    expect(admin_page.get_by_test_id("development-panel")).to_be_hidden()
    expect(admin_page).not_to_have_url(re.compile(r"#"))


def test_hash_opens_a_lazy_tab(admin_page: Page, issue_with_development: dict) -> None:
    admin_page.goto(f"/issue/{issue_with_development['key']}/#attachments")
    expect(admin_page.locator("#issue-attachments-tab")).to_be_visible()
    expect(admin_page.locator("#issue-attachments-tab")).not_to_contain_text("Loading attachments")


@pytest.mark.parametrize("width", [375, 768, 1280])
def test_tab_bar_fits_the_viewport(admin_page: Page, issue_with_development: dict, width: int) -> None:
    admin_page.set_viewport_size({"width": width, "height": 900})
    admin_page.goto(f"/issue/{issue_with_development['key']}/")
    tab = _tab(admin_page)
    tab.scroll_into_view_if_needed()
    tab.click()
    expect(admin_page.get_by_test_id("development-panel")).to_be_visible()
    if os.environ.get("SP_SHOT"):
        admin_page.locator(".activity-tabs").screenshot(path=f"{os.environ['SP_SHOT']}-{width}.png")
    # Every tab is reachable: the last one scrolls into view and opens.
    attachments = admin_page.locator('.activity-tabs-header [data-tab="attachments"]')
    attachments.scroll_into_view_if_needed()
    attachments.click()
    expect(admin_page.locator("#issue-attachments-tab")).to_be_visible()
    overflow = admin_page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 0, f"horizontal overflow of {overflow}px at {width}px"
    inner = admin_page.evaluate(
        "(() => { const m = document.querySelector('main.main-content'); return m.scrollWidth - m.clientWidth; })()"
    )
    assert inner <= 0, f"main content scrolls sideways by {inner}px at {width}px"


_GIT_JOURNAL = """
import asyncio, asyncpg, sys
async def _seed(issue_id):
    conn = await asyncpg.connect(sys.argv[1])
    try:
        project_id = await conn.fetchval("SELECT project_id FROM issues WHERE id = $1", issue_id)
        git_id = await conn.fetchval("SELECT id FROM users WHERE login = '$git'")
        seq = await conn.fetchval("SELECT coalesce(max(sequence), 0) + 1 FROM journals WHERE issue_id = $1", issue_id)
        await conn.execute(
            "INSERT INTO journals (issue_id, project_id, user_id, notes, sequence) VALUES ($1, $2, $3, $4, $5)",
            issue_id, project_id, git_id, "Linked commit 1234567", seq,
        )
    finally:
        await conn.close()
asyncio.run(_seed(int(sys.argv[2])))
"""


def test_git_account_avatar_is_the_bundled_icon(admin_page: Page, issue_with_development: dict) -> None:
    db_url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    result = subprocess.run(
        ["uv", "run", "python", "-c", _GIT_JOURNAL, db_url, str(issue_with_development["id"])],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    admin_page.goto(f"/issue/{issue_with_development['key']}/")
    entry = admin_page.locator(".journal-entry", has_text="Linked commit 1234567")
    icon = entry.locator(".sp-avatar-icon-git")
    expect(icon).to_be_visible()
    expect(icon).to_have_text("")
    box = icon.bounding_box()
    assert box is not None and round(box["width"]) == round(box["height"]) == 22
    mask = icon.evaluate(
        "el => getComputedStyle(el, '::before').maskImage || getComputedStyle(el, '::before').webkitMaskImage"
    )
    assert "/static/img/avatars/git.svg" in mask
    loaded = admin_page.evaluate(
        "fetch('/static/img/avatars/git.svg').then(r => r.ok && r.headers.get('content-type').includes('svg'))"
    )
    assert loaded
    if os.environ.get("SP_SHOT"):
        entry.screenshot(path=os.environ["SP_SHOT"])
