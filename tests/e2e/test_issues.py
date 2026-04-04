"""E2E tests for issue list, create, and detail pages."""

import httpx
import pytest
from playwright.sync_api import Page, expect

from specivo.testing.e2e_base import create_issue, create_project, unique_key
from tests.e2e.pages.issue_form_page import IssueFormPage
from tests.e2e.pages.issue_list_page import IssueListPage

pytestmark = [pytest.mark.e2e]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_project(api: httpx.Client) -> dict:
    """Create a project via API and return the response dict."""
    return create_project(api)


_create_issue = create_issue


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_issue_list_renders(admin_page: Page, api_client: httpx.Client) -> None:
    """Issue list page loads and shows the 'New Issue' button."""
    project = _create_project(api_client)
    issues = IssueListPage(admin_page)
    issues.navigate(project["key"])
    issues.expect_loaded()
    expect(issues.new_issue_button).to_be_visible()


def test_issue_list_shows_created_issue(admin_page: Page, api_client: httpx.Client) -> None:
    """An issue created via API appears in the issue list."""
    project = _create_project(api_client)
    subject = f"E2E Issue {unique_key()}"
    _create_issue(api_client, project["key"], subject)

    issues = IssueListPage(admin_page)
    issues.navigate(project["key"])
    issues.expect_issue_visible(subject)


def test_issue_list_empty_state(admin_page: Page, api_client: httpx.Client) -> None:
    """Empty project shows 'No issues found' state."""
    project = _create_project(api_client)
    issues = IssueListPage(admin_page)
    issues.navigate(project["key"])
    expect(issues.empty_state).to_be_visible()


@pytest.mark.skip(reason="Alpine.js x-model binding not triggered by Playwright fill — investigate separately")
def test_create_issue_via_form(admin_page: Page, api_client: httpx.Client) -> None:
    """Create an issue through the Alpine.js form and verify redirect."""
    project = _create_project(api_client)
    subject = f"E2E Created {unique_key()}"

    form = IssueFormPage(admin_page)
    form.navigate_new(project["key"])
    form.expect_loaded()
    form.fill_subject(subject)
    form.fill_description("Created by E2E test")
    # Wait for Alpine.js to enable the submit button
    expect(form.submit_button).to_be_enabled(timeout=3000)
    form.submit()

    # Should redirect to issue detail or issue list
    admin_page.wait_for_url(f"**/projects/{project['key']}/issues/**", timeout=10000)
    expect(admin_page.locator(f"text={subject}")).to_be_visible()


def test_new_issue_form_renders(admin_page: Page, api_client: httpx.Client) -> None:
    """New issue form loads with Alpine.js issueForm component."""
    project = _create_project(api_client)
    form = IssueFormPage(admin_page)
    form.navigate_new(project["key"])
    form.expect_loaded()
    expect(admin_page.locator(".form-card")).to_be_visible()


def test_issue_detail_page(admin_page: Page, api_client: httpx.Client) -> None:
    """Issue detail page shows issue subject and metadata."""
    project = _create_project(api_client)
    subject = f"E2E Detail {unique_key()}"
    issue = _create_issue(api_client, project["key"], subject)

    admin_page.goto(f"/projects/{project['key']}/issues/{issue['key']}/")
    expect(admin_page.locator(f"text={subject}")).to_be_visible()


def test_issue_list_filter_by_status(admin_page: Page, api_client: httpx.Client) -> None:
    """Filtering issues by status 'closed' shows no results when all are open."""
    project = _create_project(api_client)
    _create_issue(api_client, project["key"], f"Open Issue {unique_key()}")

    issues = IssueListPage(admin_page)
    issues.navigate(project["key"])
    issues.filter_by_status("closed")
    # After filtering by closed, the issue table should be empty or show empty state
    expect(admin_page.locator(".issue-table tbody tr")).to_have_count(0, timeout=5000)
