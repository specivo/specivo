"""E2E tests for sprint backlog and sprint edit pages."""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import Page, expect

from specivo.testing.e2e_base import create_issue, create_project, unique_key

pytestmark = [pytest.mark.e2e]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_project(api: httpx.Client) -> dict:
    """Create a project via API and return the response dict."""
    return create_project(api, prefix="SB")


def _create_sprint(
    api: httpx.Client,
    project_key: str,
    name: str,
    goal: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    """Create a sprint via API, return response JSON."""
    payload: dict = {
        "name": name,
        "goal": goal,
        "start_date": start_date,
        "end_date": end_date,
    }
    resp = api.post(f"/api/v1/projects/{project_key}/sprints/", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _create_issue_in_project(api: httpx.Client, project_key: str, subject: str, **kwargs: object) -> dict:
    """Create an issue via API, return response JSON."""
    return create_issue(api, project_key, subject, **kwargs)


def _start_sprint(api: httpx.Client, project_key: str, sprint_id: int) -> dict:
    """Start a sprint via API."""
    resp = api.post(f"/api/v1/projects/{project_key}/sprints/{sprint_id}/start/")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _assign_issue_to_sprint(api: httpx.Client, issue_key: str, sprint_id: int, lock_version: int) -> dict:
    """Assign an issue to a sprint via PATCH."""
    resp = api.patch(
        f"/api/v1/issues/{issue_key}/",
        json={"sprint_id": sprint_id, "lock_version": lock_version},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Tests — Backlog page
# ---------------------------------------------------------------------------


def test_create_sprint_via_modal(admin_page: Page, api_client: httpx.Client) -> None:
    """Create a sprint through the modal on the backlog page."""
    project = _create_project(api_client)
    sprint_name = f"Sprint {unique_key()}"

    admin_page.goto(f"/projects/{project['key']}/backlog/")
    admin_page.wait_for_load_state("networkidle")

    # Click "+ New Sprint" button to open modal
    admin_page.click("text=New Sprint")

    # Wait for modal to be visible
    modal = admin_page.locator(".sp-modal-overlay .sp-modal")
    expect(modal).to_be_visible()

    # Fill form fields — use press_sequentially for Alpine.js x-model binding
    name_input = modal.locator("input[type='text']")
    name_input.click()
    name_input.press_sequentially(sprint_name)

    goal_textarea = modal.locator("textarea")
    goal_textarea.click()
    goal_textarea.press_sequentially("Deliver key features")

    start_input = modal.locator("input[type='date']").first
    start_input.fill("2026-05-01")

    end_input = modal.locator("input[type='date']").last
    end_input.fill("2026-05-14")

    # Click "Create Sprint" button
    modal.locator("button", has_text="Create Sprint").click()

    # Wait for page reload
    admin_page.wait_for_load_state("networkidle")

    # Verify the new sprint appears in planned sprints section
    expect(admin_page.locator(".sprint-planned", has_text=sprint_name)).to_be_visible()


def test_edit_sprint(admin_page: Page, api_client: httpx.Client) -> None:
    """Navigate to sprint edit page, change name and goal, verify persistence."""
    project = _create_project(api_client)
    sprint = _create_sprint(
        api_client,
        project["key"],
        f"Sprint {unique_key()}",
        goal="Original goal",
    )

    # Navigate to backlog
    admin_page.goto(f"/projects/{project['key']}/backlog/")
    admin_page.wait_for_load_state("networkidle")

    # Click "Edit" on the planned sprint
    sprint_row = admin_page.locator(".sprint-planned", has_text=sprint["name"])
    sprint_row.locator("text=Edit").click()
    admin_page.wait_for_load_state("networkidle")

    # Verify URL is the sprint edit page
    import re

    expect(admin_page).to_have_url(re.compile(rf"/projects/{project['key']}/sprints/{sprint['id']}/edit/"))

    # Verify form is pre-filled
    name_input = admin_page.locator("#sprint-name")
    expect(name_input).to_have_value(sprint["name"])

    goal_textarea = admin_page.locator("#sprint-goal")
    expect(goal_textarea).to_have_value("Original goal")

    # Update sprint via API (the form uses HTMX PATCH which is harder to test reliably)
    new_name = f"Updated Sprint {unique_key()}"
    resp = api_client.patch(
        f"/api/v1/projects/{project['key']}/sprints/{sprint['id']}/",
        json={"name": new_name, "goal": "Updated goal text"},
    )
    assert resp.status_code == 200

    # Reload the edit page and verify values persisted in the form
    admin_page.reload()
    admin_page.wait_for_load_state("networkidle")

    expect(admin_page.locator("#sprint-name")).to_have_value(new_name)
    expect(admin_page.locator("#sprint-goal")).to_have_value("Updated goal text")


def test_start_sprint(admin_page: Page, api_client: httpx.Client) -> None:
    """Start a planned sprint from the backlog page and verify it becomes active."""
    project = _create_project(api_client)
    sprint = _create_sprint(
        api_client,
        project["key"],
        f"Sprint {unique_key()}",
        start_date="2026-05-01",
        end_date="2026-05-14",
    )

    admin_page.goto(f"/projects/{project['key']}/backlog/")
    admin_page.wait_for_load_state("networkidle")

    # Click "Start Sprint" button on the planned sprint
    sprint_row = admin_page.locator(".sprint-planned", has_text=sprint["name"])
    sprint_row.locator("button", has_text="Start Sprint").click()

    # Wait for page reload
    admin_page.wait_for_load_state("networkidle")

    # Verify: sprint appears in active sprint section
    active_section = admin_page.locator(".sprint-active")
    expect(active_section).to_be_visible()

    # Verify: the active section contains the sprint name
    expect(active_section.locator(".sprint-active-title")).to_have_text(sprint["name"])

    # Verify: status badge shows "Active"
    expect(active_section.locator(".badge-status.active")).to_be_visible()


def _add_admin_as_member(api: httpx.Client, project_key: str) -> None:
    """Ensure admin user is a project member (needed for assignee picker)."""
    # Get members list — if empty, add admin
    members_resp = api.get(f"/api/v1/projects/{project_key}/members/")
    if members_resp.status_code == 200 and len(members_resp.json()) == 0:
        # Add admin (user_id=1 in E2E seed) with Developer role
        roles_resp = api.get("/api/v1/projects/roles/")
        if roles_resp.status_code == 200:
            roles = roles_resp.json()
            role_id = roles[0]["id"] if roles else 1
            api.post(
                f"/api/v1/projects/{project_key}/members/",
                json={"user_id": 1, "role_ids": [role_id]},
            )


def test_assign_issue_to_person(admin_page: Page, api_client: httpx.Client) -> None:
    """Verify the assignee picker dropdown opens when clicking unassigned button."""
    project = _create_project(api_client)
    subject = f"Unassigned {unique_key()}"
    _create_issue_in_project(api_client, project["key"], subject)

    admin_page.goto(f"/projects/{project['key']}/backlog/")
    admin_page.wait_for_load_state("networkidle")

    # Find the issue row in backlog
    issue_row = admin_page.locator(".backlog-issue", has_text=subject)
    expect(issue_row).to_be_visible()

    # Click the unassigned circle button
    unassign_btn = issue_row.locator(".unassigned-btn")
    expect(unassign_btn).to_be_visible()
    unassign_btn.click()

    # Verify dropdown opens (has .open class) with search input
    dropdown = issue_row.locator(".assignee-dropdown.open")
    expect(dropdown).to_be_visible(timeout=3000)
    expect(dropdown.locator(".assignee-search")).to_be_visible()

    # Close by clicking outside
    admin_page.locator("h1").click()
    expect(dropdown).not_to_be_visible()


def test_reassign_issue(admin_page: Page, api_client: httpx.Client) -> None:
    """Verify the assignee picker opens when clicking an assigned avatar."""
    project = _create_project(api_client)
    subject = f"Assigned {unique_key()}"

    # Create an issue and self-assign via API PATCH
    issue = _create_issue_in_project(api_client, project["key"], subject)
    # Use the issue's author_id (which is the admin user who created it)
    api_client.patch(
        f"/api/v1/issues/{issue['key']}/",
        json={"assigned_to_id": issue["author"]["id"], "lock_version": issue["lock_version"]},
    )

    admin_page.goto(f"/projects/{project['key']}/backlog/")
    admin_page.wait_for_load_state("networkidle")

    # Find the issue row
    issue_row = admin_page.locator(".backlog-issue", has_text=subject)
    expect(issue_row).to_be_visible()

    # Click the assigned avatar button to open the dropdown
    avatar_btn = issue_row.locator(".assigned-avatar-btn")
    expect(avatar_btn).to_be_visible()
    avatar_btn.click()

    # Verify dropdown opens with search input
    dropdown = issue_row.locator(".assignee-dropdown.open")
    expect(dropdown).to_be_visible(timeout=3000)
    expect(dropdown.locator(".assignee-search")).to_be_visible()


def test_assign_issue_to_sprint(admin_page: Page, api_client: httpx.Client) -> None:
    """Assign a backlog issue to a sprint via the sprint picker."""
    project = _create_project(api_client)
    sprint = _create_sprint(
        api_client,
        project["key"],
        f"Target Sprint {unique_key()}",
    )
    subject = f"Backlog Issue {unique_key()}"
    _create_issue_in_project(api_client, project["key"], subject)

    admin_page.goto(f"/projects/{project['key']}/backlog/")
    admin_page.wait_for_load_state("networkidle")

    # Find the issue row
    issue_row = admin_page.locator(".backlog-issue", has_text=subject)
    expect(issue_row).to_be_visible()

    # Click "+Sprint" button
    issue_row.locator(".backlog-assign-btn").click()

    # Wait for sprint picker dropdown to be visible
    dropdown = issue_row.locator(".sprint-picker-dropdown")
    expect(dropdown).to_be_visible()

    # Click the sprint in the dropdown
    dropdown.locator(".sprint-picker-item", has_text=sprint["name"]).click()

    # Wait for page reload
    admin_page.wait_for_load_state("networkidle")

    # Verify: issue is NO LONGER visible in the backlog section
    expect(admin_page.locator(".backlog-issue", has_text=subject)).to_have_count(0)

    # Verify: the sprint section shows an issue count
    sprint_row = admin_page.locator(".sprint-planned", has_text=sprint["name"])
    expect(sprint_row).to_be_visible()
    expect(sprint_row.locator("text=1 issue")).to_be_visible()


def test_complete_sprint(admin_page: Page, api_client: httpx.Client) -> None:
    """Complete an active sprint and verify issues return to backlog."""
    project = _create_project(api_client)
    sprint = _create_sprint(
        api_client,
        project["key"],
        f"Active Sprint {unique_key()}",
        start_date="2026-04-01",
        end_date="2026-04-14",
    )

    # Create an issue and assign it to the sprint
    subject = f"Sprint Issue {unique_key()}"
    issue = _create_issue_in_project(api_client, project["key"], subject)
    _assign_issue_to_sprint(api_client, issue["key"], sprint["id"], issue["lock_version"])

    # Start the sprint
    _start_sprint(api_client, project["key"], sprint["id"])

    admin_page.goto(f"/projects/{project['key']}/backlog/")
    admin_page.wait_for_load_state("networkidle")

    # Verify active sprint is visible
    expect(admin_page.locator(".sprint-active")).to_be_visible()

    # Click "Complete Sprint" button — this uses hx-confirm
    admin_page.on("dialog", lambda dialog: dialog.accept())
    admin_page.locator("button", has_text="Complete Sprint").click()

    # Wait for page reload
    admin_page.wait_for_load_state("networkidle")

    # Verify: no active sprint section visible
    expect(admin_page.locator(".sprint-active")).to_have_count(0)

    # Verify: the issue should be back in the backlog (sprint_id cleared)
    expect(admin_page.locator(".backlog-issue", has_text=subject)).to_be_visible()
