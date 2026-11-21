"""E2E: the project settings General tab edits which metadata keys become link graph nodes."""

import httpx
import pytest
from playwright.sync_api import Page, expect

from specivo.testing.e2e_base import create_project
from tests.e2e.e2e_helpers import ConsoleErrorTracker

pytestmark = [pytest.mark.e2e]


def test_graph_metadata_keys_are_saved_from_the_general_tab(admin_page: Page, api_client: httpx.Client) -> None:
    key = create_project(api_client)["key"]
    tracker = ConsoleErrorTracker().attach(admin_page)
    admin_page.goto(f"/projects/{key}/settings/")
    field = admin_page.get_by_test_id("project-graph-metadata-keys")
    expect(field).to_have_value("component")
    expect(admin_page.get_by_label("Metadata keys in the link graph")).to_be_visible()

    field.fill("component, team ,  area")
    admin_page.get_by_test_id("project-general-save").click()
    expect(admin_page.get_by_test_id("project-general-message")).to_contain_text("Saved successfully.")
    assert api_client.get(f"/api/v1/projects/{key}/").json()["graph_metadata_keys"] == ["component", "team", "area"]

    admin_page.reload()
    expect(admin_page.get_by_test_id("project-graph-metadata-keys")).to_have_value("component, team, area")
    tracker.assert_no_errors()

    field = admin_page.get_by_test_id("project-graph-metadata-keys")
    field.fill("bad/key")
    admin_page.get_by_test_id("project-general-save").click()
    expect(admin_page.get_by_test_id("project-general-message")).to_contain_text("cannot contain '/'")
