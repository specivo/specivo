"""E2E tests for project tree (subprojects in list and detail views)."""

import httpx
import pytest
from playwright.sync_api import Page, expect

from specivo.testing.e2e_base import create_project

pytestmark = [pytest.mark.e2e]


def _create_project(api: httpx.Client, parent_key: str | None = None) -> dict:
    kwargs: dict = {}
    if parent_key:
        kwargs["parent_key"] = parent_key
        kwargs["description"] = f"Sub of {parent_key}"
    return create_project(api, **kwargs)


def test_subproject_shown_inside_parent_card(admin_page: Page, api_client: httpx.Client) -> None:
    """Subproject appears inside the parent card on the projects list."""
    parent = _create_project(api_client)
    child = _create_project(api_client, parent_key=parent["key"])

    admin_page.goto("/projects/")
    # Parent card visible (use project-card-name to avoid matching modal options)
    expect(admin_page.locator(f".project-card-name:has-text('{parent['name']}')")).to_be_visible()
    # Child visible as a subproject row
    expect(admin_page.locator(f".subproject-name:has-text('{child['name']}')")).to_be_visible()


def test_subproject_shown_as_nested_row(admin_page: Page, api_client: httpx.Client) -> None:
    """Subproject is displayed as a nested row under its parent."""
    parent = _create_project(api_client)
    child = _create_project(api_client, parent_key=parent["key"])

    admin_page.goto("/projects/")
    # Child should appear in a subproject row
    child_row = admin_page.locator(f".subproject-name:has-text('{child['name']}')")
    expect(child_row).to_be_visible()


def test_subproject_shows_description(admin_page: Page, api_client: httpx.Client) -> None:
    """Subproject description is set correctly via API."""
    parent = _create_project(api_client)
    child = _create_project(api_client, parent_key=parent["key"])

    # Verify the description was saved (shown on detail page, not project list cards)
    admin_page.goto(f"/projects/{child['key']}/")
    expect(admin_page.locator(f"text=Sub of {parent['key']}")).to_be_visible()


def test_subproject_link_navigates_to_detail(admin_page: Page, api_client: httpx.Client) -> None:
    """Clicking a subproject link navigates to the subproject detail page."""
    parent = _create_project(api_client)
    child = _create_project(api_client, parent_key=parent["key"])

    admin_page.goto("/projects/")
    admin_page.locator(f"a:has-text('{child['name']}')").click()
    admin_page.wait_for_url(f"**/projects/{child['key']}/")


def test_parent_detail_shows_subprojects_section(admin_page: Page, api_client: httpx.Client) -> None:
    """Parent project detail page has a Subprojects section listing children."""
    parent = _create_project(api_client)
    child = _create_project(api_client, parent_key=parent["key"])

    admin_page.goto(f"/projects/{parent['key']}/")
    expect(admin_page.locator("text=Subprojects")).to_be_visible()
    expect(admin_page.locator(f"text={child['name']}")).to_be_visible()


def test_project_without_children_has_no_subprojects_section(admin_page: Page, api_client: httpx.Client) -> None:
    """Project detail page without subprojects does not show the section."""
    project = _create_project(api_client)

    admin_page.goto(f"/projects/{project['key']}/")
    expect(admin_page.locator("text=Subprojects")).not_to_be_visible()
