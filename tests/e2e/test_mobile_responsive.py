"""Playwright E2E tests for responsive design across multiple viewports.

Verifies that key pages render correctly at mobile (375px), tablet (768px),
narrow (960px), and desktop (1280px) without horizontal overflow, clipped
buttons, or broken layouts.

Uses a dedicated seed project (RTEST) from conftest.responsive_project.
"""

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import VIEWPORTS

pytestmark = [pytest.mark.e2e]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _no_horizontal_overflow(page: Page) -> bool:
    """Check that the page body doesn't overflow horizontally."""
    return page.evaluate(
        "document.documentElement.scrollWidth <= document.documentElement.clientWidth + 2"
    )


def _buttons_within_viewport(page: Page, selector: str, viewport_width: int) -> None:
    """Assert all buttons matching selector are within the viewport."""
    buttons = page.locator(selector)
    for i in range(buttons.count()):
        box = buttons.nth(i).bounding_box()
        if box:
            assert box["x"] >= 0, f"Button {i} is off-screen left"
            assert box["x"] + box["width"] <= viewport_width + 10, (
                f"Button {i} extends beyond viewport ({box['x'] + box['width']} > {viewport_width})"
            )


def _grid_is_single_column(page: Page, selector: str) -> bool:
    """Check if a grid element has collapsed to a single column."""
    el = page.locator(selector)
    if el.count() == 0:
        return True
    cols = el.evaluate("el => window.getComputedStyle(el).gridTemplateColumns")
    return " " not in cols.strip()


# ---------------------------------------------------------------------------
# All-viewport tests (run at each of the 4 viewports)
# ---------------------------------------------------------------------------


class TestResponsiveProjectDetail:
    """Responsive layout for project detail page."""

    def test_no_horizontal_overflow(self, responsive_page: Page, responsive_project: str):
        responsive_page.goto(f"/projects/{responsive_project}/")
        assert _no_horizontal_overflow(responsive_page), "Project detail page has horizontal overflow"

    def test_page_header_visible(self, responsive_page: Page, responsive_project: str):
        responsive_page.goto(f"/projects/{responsive_project}/")
        responsive_page.wait_for_load_state("networkidle")
        header = responsive_page.locator(".page-header")
        expect(header).to_be_visible()

    def test_action_buttons_not_clipped(self, responsive_page: Page, responsive_project: str, viewport_size: dict):
        responsive_page.goto(f"/projects/{responsive_project}/")
        responsive_page.wait_for_load_state("networkidle")
        _buttons_within_viewport(responsive_page, ".page-header-actions .sp-btn", viewport_size["width"])


class TestResponsiveIssuesList:
    """Responsive layout for issues list page."""

    def test_no_horizontal_overflow(self, responsive_page: Page, responsive_project: str):
        responsive_page.goto(f"/projects/{responsive_project}/issues/")
        assert _no_horizontal_overflow(responsive_page), "Issues list has horizontal overflow"

    def test_filter_bar_visible(self, responsive_page: Page, responsive_project: str):
        responsive_page.goto(f"/projects/{responsive_project}/issues/")
        responsive_page.wait_for_load_state("networkidle")
        filters = responsive_page.locator(".filter-select")
        if filters.count() > 0:
            expect(filters.first).to_be_visible()

    def test_issue_table_scrollable(self, responsive_page: Page, responsive_project: str):
        responsive_page.goto(f"/projects/{responsive_project}/issues/")
        responsive_page.wait_for_load_state("networkidle")
        card = responsive_page.locator(".card")
        if card.count() > 0:
            overflow = card.first.evaluate("el => window.getComputedStyle(el).overflowX")
            assert overflow in ("auto", "scroll", "visible"), "Issue table card should handle overflow"


class TestResponsiveBoardView:
    """Responsive layout for kanban board view."""

    def test_no_horizontal_overflow(self, responsive_page: Page, responsive_project: str):
        responsive_page.goto(f"/projects/{responsive_project}/issues/?view=board")
        assert _no_horizontal_overflow(responsive_page), "Board view has horizontal overflow"

    def test_board_scrolls_horizontally(self, responsive_page: Page, responsive_project: str):
        responsive_page.goto(f"/projects/{responsive_project}/issues/?view=board")
        board = responsive_page.locator(".kanban-board")
        if board.count() > 0:
            overflow = board.evaluate("el => window.getComputedStyle(el).overflowX")
            assert overflow in ("auto", "scroll"), "Board should scroll horizontally"

    def test_progress_bar_visible(self, responsive_page: Page, responsive_project: str):
        responsive_page.goto(f"/projects/{responsive_project}/issues/?view=board")
        progress = responsive_page.locator(".board-progress")
        if progress.count() > 0:
            expect(progress).to_be_visible()


class TestResponsiveBacklog:
    """Responsive layout for sprint backlog page."""

    def test_no_horizontal_overflow(self, responsive_page: Page, responsive_project: str):
        responsive_page.goto(f"/projects/{responsive_project}/backlog/")
        assert _no_horizontal_overflow(responsive_page), "Backlog page has horizontal overflow"

    def test_content_visible(self, responsive_page: Page, responsive_project: str):
        responsive_page.goto(f"/projects/{responsive_project}/backlog/")
        content = responsive_page.locator(".main-content")
        expect(content).to_be_visible()


class TestResponsiveWiki:
    """Responsive layout for wiki pages."""

    def test_no_horizontal_overflow(self, responsive_page: Page, responsive_project: str):
        responsive_page.goto(f"/projects/{responsive_project}/wiki/home/")
        assert _no_horizontal_overflow(responsive_page), "Wiki page has horizontal overflow"

    def test_wiki_title_visible(self, responsive_page: Page, responsive_project: str):
        responsive_page.goto(f"/projects/{responsive_project}/wiki/home/")
        title = responsive_page.locator(".wiki-page-title")
        if title.count() > 0:
            expect(title).to_be_visible()

    def test_wiki_actions_visible(self, responsive_page: Page, responsive_project: str, viewport_size: dict):
        """Edit and History buttons must be visible at every viewport width."""
        responsive_page.goto(f"/projects/{responsive_project}/wiki/home/")
        responsive_page.wait_for_load_state("networkidle")
        actions = responsive_page.locator(".wiki-page-actions")
        if actions.count() > 0:
            expect(actions).to_be_visible()
            # Each action button must be within the viewport
            _buttons_within_viewport(responsive_page, ".wiki-page-actions .sp-btn", viewport_size["width"])

    def test_wiki_content_fits(self, responsive_page: Page, responsive_project: str, viewport_size: dict):
        responsive_page.goto(f"/projects/{responsive_project}/wiki/home/")
        content = responsive_page.locator(".wiki-content")
        if content.count() > 0:
            box = content.bounding_box()
            if box:
                assert box["width"] <= viewport_size["width"], "Wiki content overflows viewport"


class TestResponsiveSprintsList:
    """Responsive layout for sprints list page."""

    def test_no_horizontal_overflow(self, responsive_page: Page, responsive_project: str):
        responsive_page.goto(f"/projects/{responsive_project}/sprints/")
        assert _no_horizontal_overflow(responsive_page), "Sprints list has horizontal overflow"

    def test_sprint_grid_layout(self, responsive_page: Page, responsive_project: str, viewport_name: str):
        responsive_page.goto(f"/projects/{responsive_project}/sprints/")
        grid = responsive_page.locator(".sprint-list-grid")
        if grid.count() > 0 and viewport_name == "mobile":
            assert _grid_is_single_column(responsive_page, ".sprint-list-grid"), (
                "Sprint grid should be single column on mobile"
            )


class TestResponsiveSettings:
    """Responsive layout for project settings page."""

    def test_no_horizontal_overflow(self, responsive_page: Page, responsive_project: str):
        responsive_page.goto(f"/projects/{responsive_project}/settings/")
        assert _no_horizontal_overflow(responsive_page), "Settings page has horizontal overflow"

    def test_tabs_visible(self, responsive_page: Page, responsive_project: str):
        responsive_page.goto(f"/projects/{responsive_project}/settings/")
        tabs = responsive_page.locator("[role=tablist], .settings-tabs, .project-tabs")
        if tabs.count() > 0:
            expect(tabs.first).to_be_visible()


# ---------------------------------------------------------------------------
# Viewport-specific structural tests
# ---------------------------------------------------------------------------


class TestMobileSpecificLayout:
    """Tests that only apply at mobile viewport (375px)."""

    @pytest.fixture(autouse=True)
    def _setup(self, browser, e2e_server, _admin_auth, responsive_project):
        _, cookies = _admin_auth
        self._project = responsive_project
        context = browser.new_context(
            base_url=e2e_server,
            viewport=VIEWPORTS["mobile"],
        )
        context.add_cookies(cookies)
        self._page = context.new_page()
        yield
        self._page.close()
        context.close()

    @pytest.fixture
    def page(self):
        return self._page

    def test_sidebar_hidden(self, page: Page):
        page.goto("/")
        sidebar = page.locator("nav.sidebar")
        expect(sidebar).not_to_be_in_viewport()

    def test_hamburger_visible(self, page: Page):
        page.goto("/")
        hamburger = page.locator(".hamburger")
        expect(hamburger).to_be_visible()


class TestDesktopSpecificLayout:
    """Tests that only apply at desktop viewport (1280px)."""

    @pytest.fixture(autouse=True)
    def _setup(self, browser, e2e_server, _admin_auth, responsive_project):
        _, cookies = _admin_auth
        self._project = responsive_project
        context = browser.new_context(
            base_url=e2e_server,
            viewport=VIEWPORTS["desktop"],
        )
        context.add_cookies(cookies)
        self._page = context.new_page()
        yield
        self._page.close()
        context.close()

    @pytest.fixture
    def page(self):
        return self._page

    def test_sidebar_visible(self, page: Page):
        page.goto("/")
        sidebar = page.locator("nav.sidebar")
        expect(sidebar).to_be_visible()

    def test_hamburger_hidden(self, page: Page):
        page.goto("/")
        hamburger = page.locator(".hamburger")
        expect(hamburger).not_to_be_visible()

    def test_issue_detail_two_columns(self, page: Page):
        page.goto(f"/projects/{self._project}/issues/")
        page.wait_for_load_state("networkidle")
        first_link = page.locator("table a[href*='/issues/']").first
        if first_link.count() > 0:
            first_link.click()
            page.wait_for_load_state("networkidle")
            layout = page.locator(".issue-detail-layout")
            if layout.count() > 0:
                cols = layout.evaluate("el => window.getComputedStyle(el).gridTemplateColumns")
                assert " " in cols, f"Issue detail should be two-column on desktop, got: {cols}"
