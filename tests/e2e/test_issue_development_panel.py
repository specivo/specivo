"""E2E: the Development panel on the issue page renders linked pull requests, branches and commits.

No Celery worker runs in the E2E environment, so the commits and the pull
request are written to the database directly, as processing would store them.
"""

import os
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
            "INSERT INTO pull_requests (integration_id, number, title, url, state, source_branch, target_branch,"
            " author_login) VALUES ($1, 7, 'Add the login form', $2, 'open', 'feat/login-form', 'main',"
            " 'alex-example') RETURNING id",
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


def test_panel_renders_and_links_out(admin_page: Page, issue_with_development: dict) -> None:
    tracker = ConsoleErrorTracker().attach(admin_page)
    admin_page.goto(f"/issue/{issue_with_development['key']}/")
    panel = admin_page.get_by_test_id("development-panel")
    expect(panel).to_be_visible()

    pr_link = panel.get_by_role("link", name="#7")
    expect(pr_link).to_have_attribute("href", f"https://github.example.com/{_REPO}/pull/7")
    expect(pr_link).to_have_attribute("target", "_blank")
    expect(panel).to_contain_text("Add the login form")
    expect(panel).to_contain_text("Open")
    expect(panel.get_by_role("link", name="feat/login-form")).to_have_attribute(
        "href", f"https://github.example.com/{_REPO}/tree/feat/login-form"
    )

    # Five commits first, the rest behind "Show 2 more".
    expect(panel.get_by_text("Step 0 of the login form")).to_be_visible()
    expect(panel.get_by_text("Step 6 of the login form")).to_be_hidden()
    panel.get_by_role("button", name="Show 2 more").click()
    expect(panel.get_by_text("Step 6 of the login form")).to_be_visible()
    expect(panel).not_to_contain_text("alex@example.com")
    tracker.assert_no_errors()


@pytest.mark.parametrize("width", [375, 768, 1280])
def test_panel_fits_the_viewport(admin_page: Page, issue_with_development: dict, width: int) -> None:
    admin_page.set_viewport_size({"width": width, "height": 900})
    admin_page.goto(f"/issue/{issue_with_development['key']}/")
    expect(admin_page.get_by_test_id("development-panel")).to_be_visible()
    overflow = admin_page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 0, f"horizontal overflow of {overflow}px at {width}px"
