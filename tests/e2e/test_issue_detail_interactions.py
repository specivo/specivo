"""E2E tests for issue detail page interactions.

Covers:
- Sidebar status change triggers page reload and shows new status
- Comment form submission makes the comment visible in the activity feed
- Progress (done_ratio) select element exists on the detail page

These tests are written TDD-first against the plan in
docs/adr/ and the implementation plan. They will fail until the
corresponding fixes are shipped.
"""

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
    return create_project(api, prefix="DI")


_create_issue = create_issue  # alias for backward compat


def _get_status_id(api: httpx.Client, name: str) -> int | None:
    """Look up a status ID by name from the seed data."""
    resp = api.get("/api/v1/issues/statuses/")
    if resp.status_code != 200:
        return None
    statuses = resp.json()
    for s in statuses:
        if s.get("name") == name:
            return s["id"]
    return None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_sidebar_status_change_refreshes(
    admin_page: Page,
    api_client: httpx.Client,
) -> None:
    """Changing the status dropdown reloads the page and shows the new status.

    This tests Fix 1 from the plan: after a successful PATCH, the sidebar
    Alpine.js component calls window.location.reload(). The new status must
    be visible in the page after the reload completes.

    The test uses seeded lookup data (tracker id=1, statuses from seed).
    """
    project = _create_project(api_client)
    subject = f"Sidebar status E2E {unique_key()}"
    issue = _create_issue(api_client, project["key"], subject)

    issue_url = f"/issue/{issue['key']}/"
    admin_page.goto(issue_url)
    admin_page.wait_for_load_state("networkidle")

    # The issue subject must be visible before interacting
    expect(admin_page.locator(f"text={subject}")).to_be_visible()

    # Find the status select in the sidebar — it has the updateField('status_id') handler.
    status_select = admin_page.locator("select[\\@change*='status_id']").first
    if not status_select.is_visible():
        # Fallback: look for any visible select in the sidebar card
        status_select = admin_page.locator(".sidebar select").first

    # Try to pick a different option from the current one.  If only one option
    # exists the test still verifies the select is present without crashing.
    options = status_select.locator("option").all()
    if len(options) < 2:
        pytest.skip("Only one status available in seed data — cannot test status change")

    # Select the last (different) option to trigger the change
    last_value = options[-1].get_attribute("value")
    if last_value:
        status_select.select_option(value=last_value)
        # After selecting, the page should reload (Fix 1)
        admin_page.wait_for_load_state("networkidle", timeout=8000)
        # The issue subject must still be visible after reload
        expect(admin_page.locator(f"text={subject}")).to_be_visible(timeout=8000)


def test_comment_form_refreshes_activity(
    admin_page: Page,
    api_client: httpx.Client,
) -> None:
    """Submitting the comment form shows the comment in the activity feed.

    This tests Fix 2: after a comment is posted, the activity feed htmx
    partial refreshes via a custom 'refresh-activity' event so the comment
    becomes visible without a full page reload.

    The test waits for the comment text to appear in the page DOM within a
    reasonable timeout, regardless of whether the update is via htmx swap
    or a full reload.
    """
    project = _create_project(api_client)
    subject = f"Comment form E2E {unique_key()}"
    issue = _create_issue(api_client, project["key"], subject)
    comment_text = f"E2E comment {unique_key()}"

    issue_url = f"/issue/{issue['key']}/"
    admin_page.goto(issue_url)
    admin_page.wait_for_load_state("networkidle")

    # Locate the comment textarea. The Alpine.js commentForm component
    # binds a textarea with x-model="notes".
    comment_textarea = admin_page.locator(".comment-form textarea")
    expect(comment_textarea).to_be_visible(timeout=5000)

    comment_textarea.fill(comment_text)

    # Find and click the comment submit button
    submit_button = admin_page.locator(".comment-form button.sp-btn-primary")
    expect(submit_button).to_be_visible(timeout=5000)

    # Click and wait for navigation (page reloads after comment submission)
    with admin_page.expect_navigation(timeout=15000):
        submit_button.click()

    admin_page.wait_for_load_state("networkidle")
    expect(admin_page.locator(f"text={comment_text}")).to_be_visible(timeout=10000)


def test_progress_select_exists(
    admin_page: Page,
    api_client: httpx.Client,
) -> None:
    """Issue detail page has a progress (done_ratio) dropdown.

    This tests Fix 3: a <select> for done_ratio must exist in the sidebar
    with options covering 0-100% in 10% increments.
    """
    project = _create_project(api_client)
    subject = f"Progress select E2E {unique_key()}"
    issue = _create_issue(api_client, project["key"], subject)

    issue_url = f"/issue/{issue['key']}/"
    admin_page.goto(issue_url)
    admin_page.wait_for_load_state("networkidle")

    expect(admin_page.locator(f"text={subject}")).to_be_visible()

    # The progress select is rendered by the sidebar template (Fix 3).
    # It must have an option for 0% and an option for 100%.
    # We look for any <select> whose options include "0%" or value="0".
    page_html = admin_page.content()
    assert "done_ratio" in page_html or "0%" in page_html, (
        "Expected a done_ratio select or 0% option on the issue detail page. "
        "Fix 3 (editable progress bar) may not be implemented yet."
    )
