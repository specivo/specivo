"""E2E tests for issue autocomplete in the new-issue form Relations section.

Covers:
- Dropdown appears when typing into the autocomplete input
- Selecting a result from the dropdown populates the input with the issue key
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import Page, expect

from specivo.testing.e2e_base import create_issue, create_project

pytestmark = [pytest.mark.e2e]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_project(api: httpx.Client, prefix: str = "AC") -> dict:
    """Create a project via API and return the response dict."""
    return create_project(api, prefix=prefix)


_create_issue = create_issue


def _open_new_issue_form(page: Page, project_key: str) -> None:
    """Navigate to the project issues page and open the new-issue modal."""
    page.goto(f"/projects/{project_key}/issues/")
    # The new-issue button is typically labelled "New Issue" or has a "+" icon.
    page.locator("text=New Issue").first.click()
    # Wait for the modal to be visible by checking for the subject field.
    expect(page.locator("#subject")).to_be_visible()


def _open_relation_form(page: Page) -> None:
    """Expand the Relations section inside the new-issue modal."""
    add_relation_btn = page.locator("text=+ Add Relation")
    expect(add_relation_btn).to_be_visible()
    add_relation_btn.click()
    # The autocomplete input becomes visible after the relation form expands.
    expect(page.locator("input[placeholder='Search issues...']")).to_be_visible()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_autocomplete_dropdown_appears(admin_page: Page, api_client: httpx.Client) -> None:
    """Typing in the autocomplete input should reveal a results dropdown.

    Setup: create a project with one issue so there is a result to find.
    Then navigate to the new-issue form, click Add Relation, type the
    project key prefix, and assert the dropdown container becomes visible
    with at least one result row.
    """
    project = _create_project(api_client)
    issue = _create_issue(api_client, project["key"], "Searchable task for autocomplete")

    _open_new_issue_form(admin_page, project["key"])
    _open_relation_form(admin_page)

    autocomplete_input = admin_page.locator("input[placeholder='Search issues...']")
    # Type enough of the key to trigger results.
    autocomplete_input.fill(project["key"])
    autocomplete_input.dispatch_event("input")

    # The dropdown container is rendered by Alpine.js x-show when results arrive.
    dropdown = admin_page.locator("input[placeholder='Search issues...'] + div")
    expect(dropdown).to_be_visible(timeout=5000)

    # At least the issue we created should appear.
    issue_entry = dropdown.locator(f"text={issue['key']}")
    expect(issue_entry).to_be_visible(timeout=5000)


def test_autocomplete_selects_issue(admin_page: Page, api_client: httpx.Client) -> None:
    """Clicking a dropdown result should populate the input with the issue key.

    After selecting an item the Alpine.js `select(item)` handler sets
    `query = item.key` and `selectedKey = item.key`. The input value should
    therefore reflect the chosen key.
    """
    project = _create_project(api_client)
    issue = _create_issue(api_client, project["key"], "Issue to be selected via autocomplete")

    _open_new_issue_form(admin_page, project["key"])
    _open_relation_form(admin_page)

    autocomplete_input = admin_page.locator("input[placeholder='Search issues...']")
    autocomplete_input.fill(project["key"])
    autocomplete_input.dispatch_event("input")

    dropdown = admin_page.locator("input[placeholder='Search issues...'] + div")
    expect(dropdown).to_be_visible(timeout=5000)

    # Click the first result row that contains our issue key.
    result_row = dropdown.locator(f"text={issue['key']}").first
    expect(result_row).to_be_visible(timeout=5000)
    result_row.click()

    # After selection the input should display the chosen key.
    expect(autocomplete_input).to_have_value(issue["key"])
