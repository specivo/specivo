"""E2E tests for relation form on new issue page.

Covers the full add/remove/reset lifecycle of the Relations section
in the Alpine.js issueForm component:

- Adding a relation populates the pending list.
- Removing a relation clears it from the pending list.
- Re-opening the relation form (via "+ Add Relation") presents a fresh
  autocomplete input — the previous search query is not retained because
  the form is rendered with x-if (destroy/recreate, not show/hide).
- After removing a relation a second one can be added successfully.
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


def _setup(api: httpx.Client, prefix: str = "RF") -> tuple[str, dict]:
    """Create a project and one issue inside it via API.

    Returns (project_key, issue_dict).
    """
    project = create_project(api, prefix=prefix)
    issue = create_issue(api, project["key"], "Test target issue")
    return project["key"], issue


def _open_relation_form(page: Page) -> None:
    """Click the '+ Add Relation' button and wait for the autocomplete input."""
    add_btn = page.locator("text=+ Add Relation")
    expect(add_btn).to_be_visible()
    add_btn.click()
    expect(page.locator("input[placeholder='Search issues...']")).to_be_visible()


def _search_and_select_issue(page: Page, issue_key: str) -> None:
    """Type issue_key into the autocomplete input and click the matching result."""
    autocomplete = page.locator("input[placeholder='Search issues...']")
    autocomplete.fill(issue_key)
    autocomplete.dispatch_event("input")

    dropdown = page.locator("input[placeholder='Search issues...'] + div")
    expect(dropdown).to_be_visible(timeout=5000)

    result_row = dropdown.locator(f"text={issue_key}").first
    expect(result_row).to_be_visible(timeout=5000)
    result_row.click()

    # After selection the input reflects the chosen key.
    expect(autocomplete).to_have_value(issue_key)


def _click_add_button(page: Page) -> None:
    """Click the 'Add' button inside the relation form."""
    add_button = page.get_by_role("button", name="Add", exact=True)
    expect(add_button).to_be_enabled()
    add_button.click()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_add_relation_shows_in_pending_list(admin_page: Page, api_client: httpx.Client) -> None:
    """Adding a relation via the form should display it in the pending list.

    Steps:
    1. Create a project + target issue via API.
    2. Navigate to the new issue form for that project.
    3. Click '+ Add Relation' to reveal the relation form.
    4. Type the issue key, select from the dropdown.
    5. Click 'Add'.
    6. Assert the pending list shows the issue key.
    """
    project_key, issue = _setup(api_client)
    admin_page.goto(f"/projects/{project_key}/issues/new/")
    expect(admin_page.locator("h1", has_text="New Issue")).to_be_visible()

    _open_relation_form(admin_page)
    _search_and_select_issue(admin_page, issue["key"])
    _click_add_button(admin_page)

    # The relation form should close and the pending list should contain the key.
    pending_list = admin_page.locator("text=" + issue["key"])
    expect(pending_list).to_be_visible()

    # The type label "Relates to" should also be visible (default relation type).
    expect(admin_page.locator("text=Relates to")).to_be_visible()


def test_remove_relation_clears_from_list(admin_page: Page, api_client: httpx.Client) -> None:
    """Clicking 'Remove' on a pending relation should remove it from the list.

    Steps:
    1. Create a project + target issue via API.
    2. Add a relation to the pending list via the form.
    3. Click 'Remove'.
    4. Assert the issue key no longer appears in the pending list.
    """
    project_key, issue = _setup(api_client)
    admin_page.goto(f"/projects/{project_key}/issues/new/")
    expect(admin_page.locator("h1", has_text="New Issue")).to_be_visible()

    _open_relation_form(admin_page)
    _search_and_select_issue(admin_page, issue["key"])
    _click_add_button(admin_page)

    # Confirm it's in the list before removing.
    expect(admin_page.locator("text=" + issue["key"])).to_be_visible()

    # Click Remove.
    remove_link = admin_page.locator("a", has_text="Remove")
    expect(remove_link).to_be_visible()
    remove_link.click()

    # The issue key entry should no longer be present in the pending list.
    expect(admin_page.locator("text=" + issue["key"])).not_to_be_visible()


def test_relation_form_resets_on_reopen(admin_page: Page, api_client: httpx.Client) -> None:
    """Re-opening the relation form after adding a relation should show an empty autocomplete.

    The relation form is guarded by x-if (not x-show), so Alpine.js destroys
    and recreates the issueAutocomplete sub-component each time the form is
    opened. This means no stale query text is retained from the previous session.

    Steps:
    1. Create a project + two issues via API.
    2. Open the form, search for issue A, select it, click 'Add'.
       The form hides automatically after adding.
    3. Click '+ Add Relation' again to reopen.
    4. Assert the autocomplete input is empty (value == "").
    """
    project_key, issue_a = _setup(api_client)
    admin_page.goto(f"/projects/{project_key}/issues/new/")
    expect(admin_page.locator("h1", has_text="New Issue")).to_be_visible()

    # Add the first relation — form closes after clicking Add.
    _open_relation_form(admin_page)
    _search_and_select_issue(admin_page, issue_a["key"])
    _click_add_button(admin_page)

    # The '+ Add Relation' button should be visible again.
    expect(admin_page.locator("text=+ Add Relation")).to_be_visible()

    # Reopen the form.
    _open_relation_form(admin_page)

    # The autocomplete input must be empty — no leftover text from previous search.
    autocomplete = admin_page.locator("input[placeholder='Search issues...']")
    expect(autocomplete).to_have_value("")


def test_can_add_second_relation_after_removing_first(admin_page: Page, api_client: httpx.Client) -> None:
    """After removing a relation, a new relation can be added successfully.

    Steps:
    1. Create a project + two separate target issues via API.
    2. Add issue A as a relation, then click 'Remove' to clear it.
    3. Click '+ Add Relation', search for issue B, select it, click 'Add'.
    4. Assert issue B appears in the pending list.
    5. Assert issue A is not present (it was removed).
    """
    project_key, issue_a = _setup(api_client, prefix="RF")

    # Create a second target issue in the same project.
    issue_b = create_issue(api_client, project_key, "Second target issue")

    admin_page.goto(f"/projects/{project_key}/issues/new/")
    expect(admin_page.locator("h1", has_text="New Issue")).to_be_visible()

    # Add issue A.
    _open_relation_form(admin_page)
    _search_and_select_issue(admin_page, issue_a["key"])
    _click_add_button(admin_page)
    expect(admin_page.locator("text=" + issue_a["key"])).to_be_visible()

    # Remove issue A.
    remove_link = admin_page.locator("a", has_text="Remove")
    expect(remove_link).to_be_visible()
    remove_link.click()
    expect(admin_page.locator("text=" + issue_a["key"])).not_to_be_visible()

    # Add issue B.
    _open_relation_form(admin_page)
    _search_and_select_issue(admin_page, issue_b["key"])
    _click_add_button(admin_page)

    # Issue B is in the pending list; issue A is not.
    expect(admin_page.locator("text=" + issue_b["key"])).to_be_visible()
    expect(admin_page.locator("text=" + issue_a["key"])).not_to_be_visible()
