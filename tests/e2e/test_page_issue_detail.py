"""E2E tests for the issue detail page (/issue/{ref}/).

Covers title, description, sidebar fields, activity tab, comment form,
and attachments tab visibility.
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import Page, expect

from specivo.testing.e2e_base import create_issue, create_project
from tests.e2e.e2e_helpers import ConsoleErrorTracker
from tests.e2e.pages.issue_detail_page import IssueDetailPage

pytestmark = [pytest.mark.e2e]


# ---------------------------------------------------------------------------
# Module-scoped fixture — shared issue for read-only tests
# ---------------------------------------------------------------------------


@pytest.fixture
def issue_ref(api_client: httpx.Client) -> str:
    """Create a project and issue, return the issue display key."""
    proj = create_project(api_client, prefix="PD")
    issue = create_issue(
        api_client,
        proj["key"],
        "Detail page test issue",
        description="Test description content",
    )
    return issue["key"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_no_console_errors(admin_page: Page, issue_ref: str) -> None:
    """Loading the issue detail page produces no JS console errors."""
    tracker = ConsoleErrorTracker().attach(admin_page)
    admin_page.goto(f"/issue/{issue_ref}/")
    admin_page.wait_for_load_state("networkidle")
    tracker.assert_no_errors()


def test_title_and_description_visible(admin_page: Page, issue_ref: str) -> None:
    """Issue subject appears in h1 and description card is visible."""
    detail = IssueDetailPage(admin_page)
    detail.navigate(issue_ref)
    detail.expect_loaded(subject="Detail page test issue")
    expect(detail.description_card).to_be_visible()


def test_activity_tab_visible(admin_page: Page, issue_ref: str) -> None:
    """Activity tab button exists on the detail page."""
    detail = IssueDetailPage(admin_page)
    detail.navigate(issue_ref)
    detail.expect_loaded()
    expect(detail.activity_tab).to_be_visible()


def test_description_history_link(admin_page: Page, issue_ref: str) -> None:
    """History link is visible in the description section."""
    detail = IssueDetailPage(admin_page)
    detail.navigate(issue_ref)
    detail.expect_loaded()
    expect(detail.history_link).to_be_visible()


def test_sidebar_status_select(admin_page: Page, issue_ref: str) -> None:
    """Status dropdown exists in the sidebar."""
    admin_page.goto(f"/issue/{issue_ref}/")
    admin_page.wait_for_load_state("networkidle")
    status_select = admin_page.locator("select[\\@change*='status_id']").first
    expect(status_select).to_be_visible(timeout=5000)


def test_sidebar_assignee_select(admin_page: Page, issue_ref: str) -> None:
    """Assignee select exists in the sidebar."""
    admin_page.goto(f"/issue/{issue_ref}/")
    admin_page.wait_for_load_state("networkidle")
    assignee_select = admin_page.locator("select[\\@change*='assigned_to_id']").first
    expect(assignee_select).to_be_visible(timeout=5000)


def test_sidebar_priority_select(admin_page: Page, issue_ref: str) -> None:
    """Priority select exists in the sidebar."""
    admin_page.goto(f"/issue/{issue_ref}/")
    admin_page.wait_for_load_state("networkidle")
    priority_select = admin_page.locator("select[\\@change*='priority_id']").first
    expect(priority_select).to_be_visible(timeout=5000)


def test_sidebar_progress_select(admin_page: Page, issue_ref: str) -> None:
    """Done ratio / progress select exists in the sidebar."""
    admin_page.goto(f"/issue/{issue_ref}/")
    admin_page.wait_for_load_state("networkidle")
    progress_select = admin_page.locator("select[\\@change*='done_ratio']").first
    expect(progress_select).to_be_visible(timeout=5000)


def test_sidebar_date_inputs(admin_page: Page, issue_ref: str) -> None:
    """Start date and due date inputs exist in the sidebar."""
    admin_page.goto(f"/issue/{issue_ref}/")
    admin_page.wait_for_load_state("networkidle")
    start_date = admin_page.locator("input[type='date'][\\@change*='start_date']").first
    due_date = admin_page.locator("input[type='date'][\\@change*='due_date']").first
    expect(start_date).to_be_visible(timeout=5000)
    expect(due_date).to_be_visible(timeout=5000)


def test_sidebar_watchers(admin_page: Page, issue_ref: str) -> None:
    """Watcher section is visible in the sidebar."""
    admin_page.goto(f"/issue/{issue_ref}/")
    admin_page.wait_for_load_state("networkidle")
    watcher = admin_page.locator(".sp-watcher-chip").first
    expect(watcher).to_be_visible(timeout=5000)


def test_comment_form_visible(admin_page: Page, issue_ref: str) -> None:
    """Comment textarea and submit button are visible."""
    detail = IssueDetailPage(admin_page)
    detail.navigate(issue_ref)
    detail.expect_loaded()
    expect(detail.comment_textarea).to_be_visible()
    expect(detail.comment_submit).to_be_visible()


def test_attachments_tab(admin_page: Page, issue_ref: str) -> None:
    """Attachments tab is clickable."""
    detail = IssueDetailPage(admin_page)
    detail.navigate(issue_ref)
    detail.expect_loaded()
    expect(detail.attachments_tab).to_be_visible()
    detail.attachments_tab.click()
