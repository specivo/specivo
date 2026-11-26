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
    # The "Linked from" block, not "Possibly related" (suggested links) beside it.
    block = admin_page.locator(".sp-linked-from", has=admin_page.locator("#wiki-backlinks"))
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
    # The rebuild command stamped the project.
    expect(card.get_by_test_id("link-graph-rebuilt-at")).to_contain_text("Last rebuilt:")
    tracker.assert_no_errors()


def test_project_graph_draws_and_filters(admin_page: Page, linked: dict) -> None:
    """The project's Graph page draws its links; unticking a kind reloads the graph without it."""
    tracker = ConsoleErrorTracker().attach(admin_page)
    admin_page.goto(f"/projects/{linked['key']}/graph/")
    graph = admin_page.get_by_test_id("project-graph")
    items = graph.get_by_test_id("project-graph-items")
    # Plan the release -> Key Rotation -> the target issue, and the missing runbook.
    expect(items.locator("li")).to_have_count(4)
    expect(graph.locator("[data-graph-canvas] svg")).to_be_visible()
    expect(graph.locator("[data-graph-status]")).to_contain_text("4 items, 3 links")

    graph.locator("label", has_text="Wiki links").locator("input").uncheck()
    # Only the mention of the target issue on the wiki page is left.
    expect(items.locator("li")).to_have_count(2)
    expect(items).to_contain_text(linked["target"]["key"])
    expect(items).not_to_contain_text("Plan the release")

    # A server that does not know suggestions yet ignores the switch.
    graph.get_by_test_id("project-graph-suggestions").check()
    expect(items.locator("li")).to_have_count(2)
    tracker.assert_no_errors()


def test_project_graph_zoom_buttons(admin_page: Page, linked: dict) -> None:
    """The zoom buttons over the chart zoom in and out and reset the view."""
    tracker = ConsoleErrorTracker().attach(admin_page)
    admin_page.goto(f"/projects/{linked['key']}/graph/")
    graph = admin_page.get_by_test_id("project-graph")
    expect(graph.locator("[data-graph-status]")).to_contain_text("4 items, 3 links")
    zoom = graph.get_by_test_id("project-graph-zoom")
    expect(zoom).to_be_visible()

    def drawing() -> str:
        return str(graph.locator("[data-graph-canvas] svg").inner_html())

    admin_page.wait_for_timeout(1500)  # the layout settles
    before = drawing()
    zoom.locator('[data-graph-zoom="in"]').click()
    admin_page.wait_for_timeout(400)
    zoomed_in = drawing()
    assert zoomed_in != before
    zoom.locator('[data-graph-zoom="out"]').click()
    zoom.locator('[data-graph-zoom="out"]').click()
    admin_page.wait_for_timeout(400)
    assert drawing() != zoomed_in
    # Zoomed out, the graph covers a small part of the canvas: dragging its empty corner still moves it.
    box = graph.locator("[data-graph-canvas]").bounding_box()
    assert box is not None
    before_drag = drawing()
    x, y = box["x"] + box["width"] - 120, box["y"] + 40
    admin_page.mouse.move(x, y)
    admin_page.mouse.down()
    admin_page.mouse.move(x - 150, y + 80, steps=8)
    admin_page.mouse.up()
    admin_page.wait_for_timeout(300)
    assert drawing() != before_drag
    zoom.locator('[data-graph-zoom="reset"]').click()
    expect(graph.locator("[data-graph-canvas] svg")).to_be_visible()
    tracker.assert_no_errors()


def test_issue_graph_tab_loads_on_first_opening(admin_page: Page, linked: dict) -> None:
    tracker = ConsoleErrorTracker().attach(admin_page)
    admin_page.goto(f"/issue/{linked['source']['key']}/")
    graph = admin_page.get_by_test_id("issue-graph")
    expect(graph).to_be_hidden()
    assert admin_page.locator('script[src*="link-graph.min"]').count() == 0
    admin_page.get_by_test_id("graph-tab").click()
    expect(graph.locator("[data-graph-canvas] svg")).to_be_visible()
    # Two hops: the wiki page, then the issue it mentions and the missing runbook.
    expect(graph.get_by_test_id("issue-graph-items").locator("li")).to_have_count(4)
    expect(graph.get_by_test_id("issue-graph-items")).to_contain_text("Key Rotation")
    tracker.assert_no_errors()
