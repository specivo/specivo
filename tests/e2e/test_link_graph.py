"""E2E: links between issues and wiki pages, as a reader sees them.

- the issue's Relations tab loads "Mentioned in" on first opening;
- a wiki page's "Linked from" block loads when opened;
- the project settings Search tab shows the link graph card with its figures.

No Celery worker runs in the E2E environment, so the stored links are built
with the rebuild command, as an operator would after an import.
"""

import os
import subprocess

import httpx
import pytest
from playwright.sync_api import Page, expect

from specivo.testing.e2e_base import create_issue, create_project
from tests.e2e.e2e_helpers import ConsoleErrorTracker

pytestmark = [pytest.mark.e2e]


def _rebuild(project_key: str) -> None:
    result = subprocess.run(
        ["uv", "run", "python", "-m", "specivo.cli.rebuild_links", "--project", project_key],
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture
def linked(api_client: httpx.Client) -> dict:
    project = create_project(api_client)
    key = project["key"]
    target = create_issue(api_client, key, "Rotate the signing keys")
    source = create_issue(api_client, key, "Plan the release", description="Implements [[Key Rotation]]")
    resp = api_client.post(
        f"/api/v1/projects/{key}/wiki/",
        json={"title": "Key Rotation", "text": f"Tracked in {target['key']}. See also [[Missing Runbook]]."},
    )
    assert resp.status_code == 201, resp.text
    _rebuild(key)
    return {"key": key, "target": target, "source": source}


def test_issue_relations_tab_lists_mentions(admin_page: Page, linked: dict) -> None:
    tracker = ConsoleErrorTracker().attach(admin_page)
    admin_page.goto(f"/issue/{linked['target']['key']}/")
    mentions = admin_page.locator("#issue-mentions")
    expect(mentions).to_contain_text("Loading")
    admin_page.locator(".activity-tab", has_text="Relations").click()
    expect(mentions.locator(".sp-backlinks-row")).to_have_count(1)
    expect(mentions).to_contain_text("Key Rotation")
    expect(mentions).to_contain_text("Wiki pages")
    tracker.assert_no_errors()


def test_wiki_page_linked_from(admin_page: Page, linked: dict) -> None:
    tracker = ConsoleErrorTracker().attach(admin_page)
    admin_page.goto(f"/projects/{linked['key']}/wiki/key-rotation/")
    block = admin_page.locator(".sp-linked-from")
    expect(block.locator(".sp-backlinks-row")).to_have_count(0)
    block.locator("summary").click()
    expect(block.locator(".sp-backlinks-row")).to_have_count(1)
    expect(block).to_contain_text(linked["source"]["key"])
    tracker.assert_no_errors()


def test_settings_search_tab_shows_the_link_graph(admin_page: Page, linked: dict) -> None:
    tracker = ConsoleErrorTracker().attach(admin_page)
    admin_page.goto(f"/projects/{linked['key']}/settings/")
    admin_page.locator(".filter-tab", has_text="Search").click()
    card = admin_page.get_by_test_id("link-graph-card")
    expect(card).to_be_visible()
    stats = card.get_by_test_id("link-graph-stats")
    expect(stats).to_be_visible()
    # [[Key Rotation]], the issue key, and [[Missing Runbook]] (unresolved).
    expect(stats).to_contain_text("3")
    expect(stats).to_contain_text("[[missing-runbook]]")
    expect(card.get_by_test_id("link-graph-rebuild")).to_be_enabled()
    expect(card).to_contain_text("Last full rebuild")
    tracker.assert_no_errors()
