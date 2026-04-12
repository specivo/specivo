"""E2E tests for the issues list page (/projects/{key}/issues/).

Covers list rendering, board view toggle, kanban lanes, progress bar,
and issue visibility after API creation.
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import Page, expect

from specivo.testing.e2e_base import create_issue, create_project, unique_key
from tests.e2e.e2e_helpers import ConsoleErrorTracker
from tests.e2e.pages.issue_list_page import IssueListPage

pytestmark = [pytest.mark.e2e]


# ---------------------------------------------------------------------------
# Module-scoped fixture — shared project with issues
# ---------------------------------------------------------------------------


@pytest.fixture
def project_with_issues(api_client: httpx.Client) -> str:
    """Create a project with two issues, return the project key."""
    proj = create_project(api_client, prefix="PI")
    create_issue(api_client, proj["key"], "Test issue for board")
    create_issue(api_client, proj["key"], "Second test issue")
    return proj["key"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_no_console_errors(admin_page: Page, project_with_issues: str) -> None:
    """Loading the issues page produces no JS console errors."""
    tracker = ConsoleErrorTracker().attach(admin_page)
    admin_page.goto(f"/projects/{project_with_issues}/issues/")
    admin_page.wait_for_load_state("networkidle")
    tracker.assert_no_errors()


def test_issues_list_renders(admin_page: Page, project_with_issues: str) -> None:
    """Issues page shows h1 'Issues' and the New Issue button."""
    issues = IssueListPage(admin_page)
    issues.navigate(project_with_issues)
    issues.expect_loaded()
    expect(issues.new_issue_button).to_be_visible()


def test_board_view_toggle(admin_page: Page, project_with_issues: str) -> None:
    """Clicking the board view toggle switches to the kanban board."""
    admin_page.goto(f"/projects/{project_with_issues}/issues/")
    admin_page.wait_for_load_state("networkidle")
    toggle = admin_page.locator(".view-toggle-btn", has_text="Board")
    expect(toggle).to_be_visible()
    toggle.click()
    admin_page.wait_for_load_state("networkidle")
    expect(admin_page.locator(".kanban-column").first).to_be_visible(timeout=5000)


def test_board_lanes_visible(admin_page: Page, project_with_issues: str) -> None:
    """Board view shows kanban columns."""
    admin_page.goto(f"/projects/{project_with_issues}/issues/?view=board")
    admin_page.wait_for_load_state("networkidle")
    cols = admin_page.locator(".kanban-column")
    expect(cols.first).to_be_visible(timeout=5000)
    assert cols.count() >= 1, "Expected at least one kanban column"


def test_board_progress_bar(admin_page: Page, project_with_issues: str) -> None:
    """Board view includes a progress bar element."""
    admin_page.goto(f"/projects/{project_with_issues}/issues/?view=board")
    admin_page.wait_for_load_state("networkidle")
    expect(admin_page.locator(".board-progress").first).to_be_visible(timeout=5000)


def test_created_issue_appears(
    admin_page: Page, api_client: httpx.Client, project_with_issues: str
) -> None:
    """An issue created via API appears in the list after reload."""
    subject = f"Dynamic issue {unique_key()}"
    create_issue(api_client, project_with_issues, subject)

    issues = IssueListPage(admin_page)
    issues.navigate(project_with_issues)
    issues.expect_issue_visible(subject)
