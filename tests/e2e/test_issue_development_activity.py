"""E2E: the Development tab's summary and timeline, detaching and moving links, and environment types in the form.

No Celery worker runs in the E2E environment, so the activity is written to
the database directly, as webhook processing would store it.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor

import asyncpg
import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from specivo.testing.e2e_base import create_issue, create_project
from tests.e2e.e2e_helpers import ConsoleErrorTracker

pytestmark = [pytest.mark.e2e]

_REPO = "example-org/gadgets"
_REPO_URL = f"https://github.example.com/{_REPO}"
_COMMITS = 25  # with a pull request, a review, a build, a deployment and a release: 30 entries

_SEED = """
import asyncio, asyncpg, sys
async def _seed(issue_id, integration_id, version_id, repo_url, n_commits):
    conn = await asyncpg.connect(sys.argv[1])
    try:
        head = "f" * 40
        for n in range(n_commits):
            sha = head if n == 0 else format(n, "x").rjust(40, "0")
            commit_id = await conn.fetchval(
                "INSERT INTO repository_commits (integration_id, sha, message, url, author_name, author_email,"
                " committed_at, branch) VALUES ($1, $2, $3, $4, 'Alex Example', 'alex@example.com',"
                " now() - make_interval(days => 2, hours => $5), 'feat/gadget') RETURNING id",
                integration_id, sha, f"Gadget step {n}", f"{repo_url}/commit/{sha}", n,
            )
            await conn.execute(
                "INSERT INTO issue_commit_links (commit_id, issue_id, source, keyword) VALUES ($1, $2, 'branch', NULL)",
                commit_id, issue_id,
            )
        pr_id = await conn.fetchval(
            "INSERT INTO pull_requests (integration_id, provider, number, title, url, state, source_branch,"
            " target_branch, author_login, provider_updated_at) VALUES ($1, 'github', 12, 'Build the gadget', $2,"
            " 'open', 'feat/gadget', 'main', 'alex-example', now() - interval '30 hours') RETURNING id",
            integration_id, f"{repo_url}/pull/12",
        )
        await conn.execute(
            "INSERT INTO issue_pull_request_links (pull_request_id, issue_id, source, closing)"
            " VALUES ($1, $2, 'body', true)", pr_id, issue_id,
        )
        await conn.execute(
            "INSERT INTO pull_request_reviews (pull_request_id, reviewer_login, state, url, submitted_at)"
            " VALUES ($1, 'sam-example', 'approved', $2, now() - interval '20 hours')",
            pr_id, f"{repo_url}/pull/12#review",
        )
        await conn.execute(
            "INSERT INTO ci_runs (integration_id, provider, run_type, external_id, name, status, head_sha, branch,"
            " url, attempt, occurred_at) VALUES ($1, 'github', 'workflow_run', 4401, 'Gadget tests', 'success',"
            " $2, 'feat/gadget', $3, 1, now() - interval '10 hours')",
            integration_id, head, f"{repo_url}/actions/runs/4401",
        )
        await conn.execute(
            "INSERT INTO deployments (integration_id, provider, external_id, environment, status, sha, ref, url,"
            " deployed_at, occurred_at) VALUES ($1, 'github', 5501, 'production', 'success', $2, 'main', $3,"
            " now() - interval '2 hours', now() - interval '2 hours')",
            integration_id, head, f"{repo_url}/deployments/production",
        )
        release_id = await conn.fetchval(
            "INSERT INTO repository_releases (integration_id, provider, tag_name, name, sha, url, is_release,"
            " released_at, occurred_at) VALUES ($1, 'github', 'v2.1', '2.1', $2, $3, true,"
            " now() - interval '1 hour', now() - interval '1 hour') RETURNING id",
            integration_id, head, f"{repo_url}/releases/tag/v2.1",
        )
        await conn.execute(
            "INSERT INTO issue_release_links (release_id, issue_id, source, version_id)"
            " VALUES ($1, $2, 'tag_commit', $3)",
            release_id, issue_id, version_id,
        )
    finally:
        await conn.close()
asyncio.run(_seed(int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]), sys.argv[5], int(sys.argv[6])))
"""


def _db_url() -> str:
    return os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")


def _db_value(sql: str, *args: object) -> object:
    async def _run() -> object:
        conn = await asyncpg.connect(_db_url())
        try:
            return await conn.fetchval(sql, *args)
        finally:
            await conn.close()

    # pytest-playwright runs an event loop of its own; use a fresh one elsewhere.
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, _run()).result()


def _connect(api: httpx.Client, project_key: str) -> dict:
    resp = api.post(
        f"/api/v1/projects/{project_key}/repositories/",
        json={"provider": "github", "repo_full_name": _REPO, "repo_url": _REPO_URL},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.fixture
def dev_issue(api_client: httpx.Client) -> dict:
    """An issue with commits, a pull request, a review, a build, a deployment and a release; and a second issue."""
    project = create_project(api_client)
    issue = create_issue(api_client, project["key"], "Build the gadget")
    other = create_issue(api_client, project["key"], "Ship the gadget")
    version = api_client.post(f"/api/v1/projects/{project['key']}/versions/", json={"name": "2.1"})
    assert version.status_code == 201, version.text
    integration = _connect(api_client, project["key"])
    result = subprocess.run(
        [
            "uv",
            "run",
            "python",
            "-c",
            _SEED,
            _db_url(),
            str(issue["id"]),
            str(integration["id"]),
            str(version.json()["id"]),
            _REPO_URL,
            str(_COMMITS),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return {"project": project, "issue": issue, "other": other, "version": version.json(), "integration": integration}


def _open_tab(page: Page, key: str) -> Locator:
    page.goto(f"/issue/{key}/#development")
    panel = page.get_by_test_id("development-panel")
    expect(panel).to_be_visible()
    return panel


def _items(panel: Locator) -> Locator:
    return panel.get_by_test_id("development-item")


def test_summary_and_timeline(admin_page: Page, dev_issue: dict) -> None:
    tracker = ConsoleErrorTracker().attach(admin_page)
    panel = _open_tab(admin_page, dev_issue["issue"]["key"])
    summary = panel.get_by_test_id("development-summary")
    expect(summary.get_by_test_id("development-stat-pull_request")).to_contain_text("1 pull request")
    expect(summary.get_by_test_id("development-stat-pull_request")).to_contain_text("Open")
    expect(summary.get_by_test_id("development-stat-review")).to_contain_text("Approved")
    expect(summary.get_by_test_id("development-stat-ci")).to_contain_text("Passing")
    expect(summary.get_by_test_id("development-stat-deployment")).to_contain_text("1 deployment")
    expect(summary.get_by_test_id("development-stat-commit")).to_contain_text(f"{_COMMITS} commits")
    deployed = summary.get_by_test_id("development-deployed")
    expect(deployed).to_contain_text("Deployed to production 2 hrs ago")
    expect(deployed.locator("time")).to_have_attribute("title", re.compile(r"\d{2}:\d{2}"))
    version_link = summary.get_by_test_id("development-release-version")
    expect(version_link).to_have_text("2.1")
    expect(version_link).to_have_attribute(
        "href", f"/projects/{dev_issue['project']['key']}/versions/{dev_issue['version']['id']}/"
    )
    expect(summary.get_by_role("link", name=_REPO)).to_have_attribute("href", _REPO_URL)

    # Newest first: the release, the deployment, the build, the review, the request, then commits.
    items = _items(panel)
    expect(items).to_have_count(20)
    kinds = [items.nth(i).get_attribute("data-kind") for i in range(6)]
    assert kinds == ["release", "deployment", "ci", "review", "pull_request", "commit"]
    expect(panel).not_to_contain_text("alex@example.com")
    if os.environ.get("SP_SHOT"):
        admin_page.get_by_test_id("development-tab-panel").screenshot(path=f"{os.environ['SP_SHOT']}-1280.png")
    tracker.assert_no_errors()


def test_kind_filter_and_show_more(admin_page: Page, dev_issue: dict) -> None:
    tracker = ConsoleErrorTracker().attach(admin_page)
    panel = _open_tab(admin_page, dev_issue["issue"]["key"])
    filters = panel.get_by_test_id("development-filters")
    builds = filters.locator('[data-kind="ci"]')
    builds.click()
    expect(builds).to_have_attribute("aria-pressed", "true")
    expect(_items(panel)).to_have_count(1)
    expect(_items(panel).first).to_have_attribute("data-kind", "ci")
    expect(panel.get_by_test_id("development-more")).to_have_count(0)
    # A second kind adds to the selection.
    filters.locator('[data-kind="deployment"]').click()
    expect(_items(panel)).to_have_count(2)
    filters.locator('[data-kind=""]').click()
    expect(filters.locator('[data-kind=""]')).to_have_attribute("aria-pressed", "true")
    expect(_items(panel)).to_have_count(20)

    # "Show more" appends the next page.
    more = panel.get_by_test_id("development-more")
    expect(more).to_have_text("Show 10 more")
    more.click()
    expect(_items(panel)).to_have_count(_COMMITS + 5)
    expect(panel.get_by_test_id("development-more")).to_have_count(0)
    expect(panel.get_by_text("Gadget step 24")).to_be_visible()

    # Commits only: two pages of commits.
    filters.locator('[data-kind="commit"]').click()
    expect(_items(panel)).to_have_count(20)
    panel.get_by_test_id("development-more").click()
    expect(_items(panel)).to_have_count(_COMMITS)
    tracker.assert_no_errors()


def _menu(panel: Locator, kind: str, label: str) -> Locator:
    item = panel.locator(f'[data-dev-kind="{kind}"][data-dev-label="{label}"]')
    expect(item).to_have_count(1)
    item.get_by_test_id("development-item-menu").click()
    return item


def test_detach_removes_the_commit_for_good(admin_page: Page, dev_issue: dict) -> None:
    tracker = ConsoleErrorTracker().attach(admin_page)
    key = dev_issue["issue"]["key"]
    panel = _open_tab(admin_page, key)
    tab_count = admin_page.locator("#development-tab-count")
    expect(tab_count).to_have_text(str(_COMMITS + 1))
    label = "f" * 7
    item = _menu(panel, "commit", label)
    messages: list[str] = []

    def accept(dialog) -> None:
        messages.append(dialog.message)
        dialog.accept()

    admin_page.once("dialog", accept)
    item.get_by_test_id("development-detach").click()
    expect(panel.locator(f'[data-dev-label="{label}"]')).to_have_count(0)
    assert messages and label in messages[0]
    expect(tab_count).to_have_text(str(_COMMITS))
    expect(panel.get_by_test_id("development-stat-commit")).to_contain_text(f"{_COMMITS - 1} commits")
    # Still gone after a reload.
    panel = _open_tab(admin_page, key)
    expect(panel.locator(f'[data-dev-label="{label}"]')).to_have_count(0)
    tracker.assert_no_errors()


def test_cancelled_detach_keeps_the_commit(admin_page: Page, dev_issue: dict) -> None:
    panel = _open_tab(admin_page, dev_issue["issue"]["key"])
    item = _menu(panel, "commit", "f" * 7)
    admin_page.once("dialog", lambda dialog: dialog.dismiss())
    item.get_by_test_id("development-detach").click()
    expect(panel.locator(f'[data-dev-label="{"f" * 7}"]')).to_have_count(1)


def test_move_puts_the_pull_request_on_the_target(admin_page: Page, dev_issue: dict) -> None:
    tracker = ConsoleErrorTracker().attach(admin_page)
    key, target = dev_issue["issue"]["key"], dev_issue["other"]["key"]
    panel = _open_tab(admin_page, key)
    item = _menu(panel, "pull_request", "#12")
    item.get_by_test_id("development-move").click()
    dialog = admin_page.get_by_test_id("development-move-dialog")
    expect(dialog).to_be_visible()
    field = dialog.get_by_test_id("development-move-target")
    expect(field).to_be_focused()
    field.fill(target)
    dialog.get_by_test_id("development-move-submit").click()
    expect(dialog).to_have_count(0)
    expect(panel.locator('[data-dev-kind="pull_request"]')).to_have_count(0)
    expect(panel.get_by_test_id("development-stat-pull_request")).to_have_count(0)

    target_panel = _open_tab(admin_page, target)
    expect(target_panel.locator('[data-dev-kind="pull_request"][data-dev-label="#12"]')).to_have_count(1)
    expect(target_panel).to_contain_text("Build the gadget")
    tracker.assert_no_errors()


def test_move_outside_the_scope_shows_the_error(admin_page: Page, api_client: httpx.Client, dev_issue: dict) -> None:
    elsewhere = create_issue(api_client, create_project(api_client)["key"], "Somewhere else")
    panel = _open_tab(admin_page, dev_issue["issue"]["key"])
    item = _menu(panel, "commit", "f" * 7)
    item.get_by_test_id("development-move").click()
    dialog = admin_page.get_by_test_id("development-move-dialog")
    field = dialog.get_by_test_id("development-move-target")
    field.fill(elsewhere["key"])
    field.press("Enter")
    error = dialog.get_by_test_id("development-move-error")
    expect(error).to_have_text("The target issue is not in a project this repository may link to")
    expect(field).to_have_attribute("aria-invalid", "true")
    expect(field).to_have_attribute("aria-describedby", "dev-move-target-error")
    # Nothing moved.
    dialog.get_by_role("button", name="Cancel").click()
    expect(panel.locator(f'[data-dev-label="{"f" * 7}"]')).to_have_count(1)


def test_viewer_without_edit_permission_gets_no_controls(
    auth_page: Page, api_client: httpx.Client, dev_issue: dict
) -> None:
    role = f"DevView {dev_issue['project']['key']}"
    role_id = _db_value(
        "INSERT INTO roles (name, permissions) VALUES ($1, '[\"view_issues\"]'::jsonb) RETURNING id", role
    )
    user_id = _db_value("SELECT id FROM users WHERE lower(login) = 'e2e_user'")
    resp = api_client.post(
        f"/api/v1/projects/{dev_issue['project']['key']}/members/", json={"user_id": user_id, "role_ids": [role_id]}
    )
    assert resp.status_code in (200, 201), resp.text
    panel = _open_tab(auth_page, dev_issue["issue"]["key"])
    expect(_items(panel)).to_have_count(20)
    expect(panel.get_by_test_id("development-item-menu")).to_have_count(0)
    expect(panel.locator("[data-dev-id]")).to_have_count(0)
    expect(auth_page.get_by_test_id("development-move-dialog")).to_have_count(0)


def test_mobile_width_has_no_horizontal_scroll(admin_page: Page, dev_issue: dict) -> None:
    admin_page.set_viewport_size({"width": 375, "height": 812})
    panel = _open_tab(admin_page, dev_issue["issue"]["key"])
    expect(_items(panel)).to_have_count(20)
    _menu(panel, "pull_request", "#12")
    expect(panel.get_by_test_id("development-detach").first).to_be_visible()
    if os.environ.get("SP_SHOT"):
        admin_page.screenshot(path=f"{os.environ['SP_SHOT']}-375.png", full_page=True)
    overflow = admin_page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 0, f"horizontal overflow of {overflow}px at 375px"
    # The page's scroll box must not scroll sideways either (the tab row once widened the column).
    inner = admin_page.evaluate(
        "(() => { const m = document.querySelector('main.main-content'); return m.scrollWidth - m.clientWidth; })()"
    )
    assert inner <= 0, f"main content scrolls sideways by {inner}px at 375px"


# ---------------------------------------------------------------------------
# Environment types in the repository form
# ---------------------------------------------------------------------------


def _open_edit(page: Page, project_key: str) -> Locator:
    page.goto(f"/projects/{project_key}/settings/")
    page.get_by_role("link", name="Repositories").click()
    card = page.get_by_test_id("repositories-card")
    expect(card).to_be_visible()
    card.get_by_role("button", name="Edit").first.click()
    modal = page.locator(".sp-modal").filter(has=page.get_by_test_id("repository-save"))
    expect(modal).to_be_visible()
    return modal


def test_environment_types_save_and_validate(admin_page: Page, api_client: httpx.Client) -> None:
    tracker = ConsoleErrorTracker().attach(admin_page)
    project = create_project(api_client)
    integration = _connect(api_client, project["key"])
    sent: list[dict] = []
    admin_page.on(
        "request",
        lambda req: (
            sent.append(req.post_data_json)
            if req.method == "PATCH" and f"/repositories/{integration['id']}/" in req.url
            else None
        ),
    )
    modal = _open_edit(admin_page, project["key"])
    envs = modal.get_by_test_id("repository-environments")
    envs.get_by_test_id("repository-env-add").click()
    name = envs.get_by_test_id("repository-env-name").nth(0)
    expect(name).to_be_focused()
    name.fill("github-pages")
    # A type is missing: the row's type is marked, nothing is sent.
    modal.get_by_test_id("repository-save").click()
    type_field = envs.get_by_test_id("repository-env-type").nth(0)
    expect(type_field).to_have_attribute("aria-invalid", "true")
    expect(envs.get_by_test_id("repository-env-type-error").nth(0)).to_have_text("Choose a type.")
    expect(type_field).to_be_focused()
    type_field.select_option("production")
    expect(type_field).not_to_have_attribute("aria-invalid", "true")

    # The same name twice (any case) is marked on the second row.
    envs.get_by_test_id("repository-env-add").click()
    second = envs.get_by_test_id("repository-env-name").nth(1)
    second.fill("GitHub-Pages")
    envs.get_by_test_id("repository-env-type").nth(1).select_option("staging")
    modal.get_by_test_id("repository-save").click()
    expect(second).to_have_attribute("aria-invalid", "true")
    expect(envs.get_by_test_id("repository-env-name-error").nth(1)).to_have_text("This environment is listed twice.")
    assert sent == []

    second.fill("qa*")
    modal.get_by_test_id("repository-save").click()
    expect(modal).to_have_count(0)
    assert sent and sent[-1]["settings"]["environment_types"] == {"github-pages": "production", "qa*": "staging"}
    stored = api_client.get(f"/api/v1/projects/{project['key']}/repositories/{integration['id']}/").json()
    assert stored["settings"]["environment_types"] == {"github-pages": "production", "qa*": "staging"}

    # Reopened, the rows are there; removing one saves the rest.
    admin_page.reload()
    modal = _open_edit(admin_page, project["key"])
    envs = modal.get_by_test_id("repository-environments")
    expect(envs.get_by_test_id("repository-env-name")).to_have_count(2)
    expect(envs.get_by_test_id("repository-env-name").nth(0)).to_have_value("github-pages")
    expect(envs.get_by_test_id("repository-env-type").nth(1)).to_have_value("staging")
    envs.get_by_test_id("repository-env-remove").nth(1).click()
    modal.get_by_test_id("repository-save").click()
    expect(modal).to_have_count(0)
    stored = api_client.get(f"/api/v1/projects/{project['key']}/repositories/{integration['id']}/").json()
    assert stored["settings"]["environment_types"] == {"github-pages": "production"}
    tracker.assert_no_errors()


def test_environment_type_server_error_shows_on_the_list(admin_page: Page, api_client: httpx.Client) -> None:
    project = create_project(api_client)
    _connect(api_client, project["key"])
    admin_page.route(
        re.compile(r".*/api/v1/projects/[^/]+/repositories/\d+/$"),
        lambda route: (
            route.fulfill(
                status=422,
                content_type="application/json",
                body='{"errors": [{"code": "validation_error", "message": "Value error, environment_types holds at most'
                ' 100 entries", "field": "settings.environment_types", "details": null}]}',
            )
            if route.request.method == "PATCH"
            else route.continue_()
        ),
    )
    modal = _open_edit(admin_page, project["key"])
    envs = modal.get_by_test_id("repository-environments")
    envs.get_by_test_id("repository-env-add").click()
    envs.get_by_test_id("repository-env-name").fill("pages")
    envs.get_by_test_id("repository-env-type").select_option("production")
    modal.get_by_test_id("repository-save").click()
    error = envs.get_by_test_id("repository-env-list-error")
    expect(error).to_have_text("environment_types holds at most 100 entries")
    # Changing the list clears it.
    envs.get_by_test_id("repository-env-name").fill("pages-eu")
    expect(error).to_be_hidden()
