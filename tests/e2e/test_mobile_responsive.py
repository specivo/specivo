"""Playwright E2E tests for responsive design across multiple viewports.

Verifies that key pages render correctly at mobile (375px), tablet (768px),
narrow (960px), and desktop (1280px) without horizontal overflow, clipped
buttons, or broken layouts.

Uses a dedicated seed project (RTEST) from conftest.responsive_project.
"""

import json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import VIEWPORTS

pytestmark = [pytest.mark.e2e]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _no_horizontal_overflow(page: Page) -> bool:
    """Check that the page body doesn't overflow horizontally."""
    return page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth + 2")


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


class TestResponsiveSimilarIssues:
    """The similar-issues list under the new issue form's subject, with long rows."""

    def test_five_rows_fit(self, responsive_page: Page, responsive_project: str, viewport_size: dict):
        long_subject = ("Login page timeout error on a very long subject line " * 6)[:300]
        items = [
            {
                "key": f"{responsive_project}-{n}",
                "subject": long_subject if n == 1 else f"Login page fails with timeout error {n}",
                "status": {"id": 1, "name": "Closed" if n == 5 else "New"},
                "is_closed": n == 5,
                "score": 0.5,
            }
            for n in range(1, 6)
        ]
        responsive_page.route(
            re.compile(r"/issues/similar/"),
            lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(items)),
        )
        responsive_page.clock.install()
        responsive_page.goto(f"/projects/{responsive_project}/issues/new/")
        responsive_page.wait_for_function("() => window.Alpine !== undefined")
        responsive_page.clock.pause_at(responsive_page.evaluate("Date.now()") + 10)
        responsive_page.locator("#subject").fill("Login page fails with timeout error")
        responsive_page.clock.run_for(1000)
        responsive_page.wait_for_timeout(150)
        responsive_page.clock.run_for(50)  # x-show applies on the next animation frame

        expect(responsive_page.locator("[data-testid=similar-issue]")).to_have_count(5)
        assert _no_horizontal_overflow(responsive_page), "New issue form has horizontal overflow"
        _buttons_within_viewport(responsive_page, ".sp-similar-issues-mark", viewport_size["width"])


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


class TestResponsiveAiUsage:
    """The AI usage pages: the project tab, My AI usage and the entry form, with a long model name."""

    @pytest.fixture(autouse=True)
    def _entry(self, api_client, responsive_project: str) -> None:
        resp = api_client.get(f"/api/v1/projects/{responsive_project}/ai-usage/")
        if resp.status_code == 200 and resp.json()["total_count"]:
            return
        resp = api_client.post(
            f"/api/v1/projects/{responsive_project}/ai-usage/",
            json={
                "tool": "claude-code",
                "model": "a-rather-long-model-name-from-a-local-runtime-2026-03-10-instruct-q4",
                "usage_date": "2026-03-10",
                "tokens": {"input": 1_234_567, "output": 89_012, "cache_read": 4_500_000},
                "cost_type": "api",
                "cost": {"amount": "12.50", "currency": "EUR"},
                "comments": "Responsive check: a long comment that wraps inside its cell, not widening the page",
            },
        )
        assert resp.status_code == 201, resp.text

    @pytest.mark.parametrize("path", ["/projects/{key}/ai-usage/", "/my/ai-usage/", "/projects/{key}/ai-usage/new/"])
    def test_no_horizontal_overflow(self, responsive_page: Page, responsive_project: str, path: str):
        responsive_page.goto(path.format(key=responsive_project))
        responsive_page.wait_for_load_state("networkidle")
        assert _no_horizontal_overflow(responsive_page), f"{path} has horizontal overflow"

    @pytest.mark.parametrize("path", ["/projects/{key}/ai-usage/", "/my/ai-usage/"])
    def test_header_actions_not_clipped(
        self, responsive_page: Page, responsive_project: str, viewport_size: dict, path: str
    ):
        responsive_page.goto(path.format(key=responsive_project))
        responsive_page.wait_for_load_state("networkidle")
        expect(responsive_page.get_by_test_id("aiu-totals")).to_be_visible()
        _buttons_within_viewport(responsive_page, ".page-header-actions .sp-btn", viewport_size["width"])

    def test_form_fields_fit(self, responsive_page: Page, responsive_project: str, viewport_size: dict):
        responsive_page.goto(f"/projects/{responsive_project}/ai-usage/new/")
        responsive_page.wait_for_load_state("networkidle")
        _buttons_within_viewport(responsive_page, ".sp-aiu-form .form-input", viewport_size["width"])
        _buttons_within_viewport(responsive_page, ".sp-aiu-form-actions .sp-btn", viewport_size["width"])


class TestResponsiveAiSpendReport:
    """The AI spend reports (project and admin): the ECharts chart fits every viewport, light and dark."""

    MARCH = "from_date=2026-03-01&to_date=2026-03-31"
    LONG_MODEL = "a-rather-long-model-name-from-a-local-runtime-2026-03-10-instruct-q4"

    @pytest.fixture(autouse=True)
    def _entries(self, api_client, responsive_project: str) -> None:
        resp = api_client.get(f"/api/v1/projects/{responsive_project}/ai-usage/", params={"model": "claude-haiku-4-5"})
        if resp.status_code == 200 and resp.json()["total_count"]:
            return
        for model, day, amount in (
            (self.LONG_MODEL, "2026-03-03", "4.20"),
            ("claude-haiku-4-5", "2026-03-12", "1.10"),
            ("claude-haiku-4-5", "2026-03-24", "0.75"),
        ):
            resp = api_client.post(
                f"/api/v1/projects/{responsive_project}/ai-usage/",
                json={
                    "tool": "claude-code",
                    "model": model,
                    "usage_date": day,
                    "tokens": {"input": 200_000, "output": 9_000},
                    "cost_type": "api",
                    "cost": {"amount": amount, "currency": "USD"},
                },
            )
            assert resp.status_code == 201, resp.text

    @pytest.mark.parametrize("scheme", ["light", "dark"])
    @pytest.mark.parametrize(
        "path",
        [
            "/projects/{key}/ai-usage/report/?{march}&group=model&period=week",
            "/projects/{key}/ai-usage/report/?{march}&group=model",
            "/admin/ai-spend/?{march}&group=project&period=day",
        ],
        ids=["project-per-week", "project-by-model", "admin-per-day"],
    )
    def test_chart_fits(
        self, responsive_page: Page, responsive_project: str, viewport_size: dict, path: str, scheme: str
    ):
        page = responsive_page
        page.emulate_media(color_scheme=scheme)
        page.goto(path.format(key=responsive_project, march=self.MARCH))
        chart = page.get_by_test_id("spend-chart")
        expect(chart).to_have_attribute("data-sp-drawn", "")
        expect(chart.locator("svg").first).to_be_visible()
        page.wait_for_load_state("networkidle")
        assert _no_horizontal_overflow(page), f"{path} has horizontal overflow"
        svg = chart.locator("svg").first.bounding_box()
        card = page.locator(".sp-spend-chart-card").bounding_box()
        assert svg and card
        assert svg["x"] >= card["x"] - 1 and svg["x"] + svg["width"] <= card["x"] + card["width"] + 1
        assert svg["x"] + svg["width"] <= viewport_size["width"]
        # No axis or legend text sticks out of the drawing.
        outside = chart.evaluate(
            """(el) => {
                const box = el.querySelector('svg').getBoundingClientRect();
                return Array.from(el.querySelectorAll('svg text')).filter((t) => {
                    if (t.closest('[clip-path]')) return false;  // legend items scrolled out of view
                    const r = t.getBoundingClientRect();
                    return r.width && (r.left < box.left - 1 || r.right > box.right + 1);
                }).map((t) => t.textContent);
            }"""
        )
        assert outside == [], f"chart text clipped at {viewport_size['width']}px: {outside}"
