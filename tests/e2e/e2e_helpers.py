"""Shared E2E test helpers — console error tracking and common UI assertions.

Every page-centric test file should use ConsoleErrorTracker in its first test
to catch JS errors (e.g. wrong Alpine.js build, broken inline scripts).

Shared component assertions (header search, avatar popup, sidebar) are tested
from each page file without duplicating assertion logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from playwright.sync_api import Page, expect

# Console messages that are not real errors (browser extensions, dev tooling)
CONSOLE_ALLOWLIST: list[str] = [
    "Download the React DevTools",
    "Manifest:",
    "[HMR]",
    "favicon",
]


@dataclass
class ConsoleErrorTracker:
    """Collects JS console errors and uncaught exceptions during a test.

    Usage::

        tracker = ConsoleErrorTracker().attach(page)
        page.goto("/some-page/")
        page.wait_for_load_state("networkidle")
        tracker.assert_no_errors()
    """

    errors: list[str] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)

    def _on_console(self, msg) -> None:
        if msg.type == "error":
            text = msg.text
            if not any(pattern in text for pattern in CONSOLE_ALLOWLIST):
                self.errors.append(text)

    def _on_pageerror(self, error) -> None:
        self.page_errors.append(str(error))

    def attach(self, page: Page) -> ConsoleErrorTracker:
        """Start listening for console errors on *page*."""
        page.on("console", self._on_console)
        page.on("pageerror", self._on_pageerror)
        return self

    def assert_no_errors(self) -> None:
        """Fail the test if any JS errors were collected."""
        all_errors = self.errors + self.page_errors
        assert not all_errors, "JS console errors detected:\n" + "\n".join(
            f"  - {e}" for e in all_errors
        )


# ---------------------------------------------------------------------------
# Shared component assertions
# ---------------------------------------------------------------------------


def assert_header_search(page: Page) -> None:
    """Verify the global search field is visible in the header."""
    expect(page.locator("#global-search")).to_be_visible()


def assert_avatar_popup(page: Page) -> None:
    """Open avatar dropdown and verify Profile / Preferences / API Keys / Sign out."""
    trigger = page.locator(".header-user")
    trigger.click()
    dropdown = page.locator(".user-dropdown")
    expect(dropdown).to_be_visible()
    expect(dropdown.locator("a[href='/my/profile/']")).to_be_visible()
    expect(dropdown.locator("a[href='/my/preferences/']")).to_be_visible()
    expect(dropdown.locator("a[href='/my/api-keys/']")).to_be_visible()
    expect(dropdown.locator("a[href='/logout/']")).to_be_visible()
    # Close dropdown by pressing Escape
    page.keyboard.press("Escape")


def assert_global_sidebar(page: Page, *, is_admin: bool = False) -> None:
    """Verify global sidebar items: Search, Dashboard, Projects (+ Admin)."""
    sidebar = page.locator("nav.sidebar")
    expect(sidebar.locator("a[href='/search/']")).to_be_visible()
    expect(sidebar.locator("a.sidebar-item[href='/']")).to_be_visible()
    expect(sidebar.locator("a[href='/projects/']")).to_be_visible()
    if is_admin:
        expect(sidebar.locator("a[href='/admin/']")).to_be_visible()


def assert_project_sidebar(page: Page, project_key: str) -> None:
    """Verify project sidebar items when inside a project context."""
    sidebar = page.locator("nav.sidebar")
    base = f"/projects/{project_key}"
    expect(sidebar.locator(f"a[href='{base}/issues/']")).to_be_visible()
    expect(sidebar.locator(f"a[href='{base}/backlog/']")).to_be_visible()
    expect(sidebar.locator(f"a[href='{base}/wiki/']")).to_be_visible()
    expect(sidebar.locator(f"a[href='{base}/roadmap/']")).to_be_visible()
    expect(sidebar.locator(f"a[href='{base}/time-entries/']")).to_be_visible()
    expect(sidebar.locator(f"a[href='{base}/sprints/']")).to_be_visible()
    expect(sidebar.locator(f"a[href='{base}/settings/']")).to_be_visible()
