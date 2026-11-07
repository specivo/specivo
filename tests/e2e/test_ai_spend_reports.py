"""E2E: the AI spend reports in the browser: the chart draws, presets and grouping work, cells drill through.

The charts are Apache ECharts with the SVG renderer: the tests hover a bar for
its tooltip, toggle a series from the legend, follow a theme change and watch
for Content-Security-Policy violations.

The shared admin account logs its own usage in a fresh project per test, so
the project report shows only what the test wrote.
"""

from __future__ import annotations

import re

import httpx
import pytest
from playwright.sync_api import Page, expect

from specivo.testing.e2e_base import create_project, unique_key
from tests.e2e.e2e_helpers import ConsoleErrorTracker

pytestmark = [pytest.mark.e2e]

MARCH = "from_date=2026-03-01&to_date=2026-03-31"

# Records every Content-Security-Policy violation of the page, before any page script runs.
CSP_WATCH = """
window.__spCspViolations = [];
document.addEventListener('securitypolicyviolation', (e) => {
  window.__spCspViolations.push(e.violatedDirective + ' ' + (e.blockedURI || ''));
});
"""

# Bars of one series: SVG paths filled with the series colour, legend icon excluded.
SERIES_BARS = """([chart, color]) => {
  const norm = (c) => { const d = document.createElement('div'); d.style.color = c;
    document.body.appendChild(d); const v = getComputedStyle(d).color; d.remove(); return v; };
  const want = norm(color);
  const box = chart.getBoundingClientRect();
  return Array.from(chart.querySelectorAll('svg path')).filter((p) => {
    const fill = p.getAttribute('fill');
    if (!fill || fill === 'none' || norm(fill) !== want) return false;
    const r = p.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && r.top > box.top + 36;  // below the legend row
  }).map((p) => { const r = p.getBoundingClientRect(); return [r.x + r.width / 2, r.y + r.height / 2]; });
}"""


def _palette(page: Page, n: int) -> str:
    return page.evaluate(f"getComputedStyle(document.documentElement).getPropertyValue('--sp-chart-{n}').trim()")


def _bars(page: Page, chart, color: str) -> list[list[float]]:
    """Viewport centres of the bars of one series, once the chart is in view and done animating."""
    chart.scroll_into_view_if_needed()
    page.wait_for_timeout(500)  # the 300 ms entry animation moves the bars
    return page.evaluate(SERIES_BARS, [chart.element_handle(), color])


def _wait_bars(page: Page, chart, color: str, count: int) -> None:
    page.wait_for_function(
        f"([c, col]) => ({SERIES_BARS})([c, col]).length === {count}", arg=[chart.element_handle(), color]
    )


def _assert_no_csp_violations(page: Page) -> None:
    violations = page.evaluate("window.__spCspViolations || []")
    assert violations == [], f"CSP violations: {violations}"


@pytest.fixture
def project(api_client: httpx.Client) -> dict:
    return create_project(api_client, prefix="ASR")


def _log(api: httpx.Client, project_key: str, model: str, day: str, amount: str, comment: str) -> dict:
    resp = api.post(
        f"/api/v1/projects/{project_key}/ai-usage/",
        json={
            "tool": "claude-code",
            "model": model,
            "usage_date": day,
            "tokens": {"input": 100_000, "output": 5_000},
            "cost_type": "api",
            "cost": {"amount": amount, "currency": "USD"},
            "comments": comment,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_project_report_chart_and_drill_down(admin_page: Page, api_client: httpx.Client, project: dict) -> None:
    key = project["key"]
    comment = unique_key("Report session ")
    _log(api_client, key, "claude-sonnet-4-5", "2026-03-03", "1.50", comment)
    _log(api_client, key, "claude-sonnet-4-5", "2026-03-12", "2.00", comment)
    _log(api_client, key, "claude-haiku-4-5", "2026-03-12", "0.25", comment)
    console = ConsoleErrorTracker().attach(admin_page)

    admin_page.goto(f"/projects/{key}/ai-usage/")
    admin_page.get_by_test_id("aiu-report-link").click()
    admin_page.wait_for_url(f"**/projects/{key}/ai-usage/report/**")
    expect(admin_page.get_by_test_id("spend-presets")).to_be_visible()

    admin_page.add_init_script(CSP_WATCH)
    admin_page.goto(f"/projects/{key}/ai-usage/report/?{MARCH}&group=model&period=week")
    rows = admin_page.get_by_test_id("spend-row")
    expect(rows).to_have_count(3)  # sonnet in two weeks, haiku in one
    expect(admin_page.get_by_test_id("spend-total")).to_contain_text("3.75")

    # Group by model over the whole range: the value by model is drawn with spend
    # against API-equivalent value per model.
    admin_page.goto(f"/projects/{key}/ai-usage/report/?{MARCH}&group=model")
    expect(rows).to_have_count(2)
    chart = admin_page.get_by_test_id("aiu-chart-models")
    expect(chart).to_have_attribute("data-sp-drawn", "")
    expect(chart.locator("svg").first).to_be_visible()
    expect(chart).to_have_attribute("role", "img")
    expect(chart.locator("svg text", has_text="claude-sonnet-4-5").first).to_be_visible()
    # The test instance has no model prices, so the API-equivalent bars are
    # empty: hover and toggle the spend series.
    spend_color = _palette(admin_page, 1)
    _wait_bars(admin_page, chart, spend_color, 2)
    x, y = _bars(admin_page, chart, spend_color)[0]
    admin_page.mouse.move(x, y)
    tooltip = chart.locator(".sp-chart-tooltip")
    expect(tooltip).to_be_visible()
    expect(tooltip).to_contain_text("Pay-per-use")
    expect(tooltip).to_contain_text("API-equivalent value")

    # The legend toggles a series off and on again.
    admin_page.mouse.move(0, 0)
    legend_spend = chart.locator("svg text").filter(has_text=re.compile(r"^Pay-per-use$")).first
    legend_spend.click()
    _wait_bars(admin_page, chart, spend_color, 0)
    legend_spend.click()
    _wait_bars(admin_page, chart, spend_color, 2)

    # Colours come from the theme's custom properties and follow a change.
    admin_page.evaluate("document.documentElement.style.setProperty('--sp-text-primary', 'rgb(1, 2, 3)')")
    admin_page.wait_for_function(
        """(c) => Array.from(c.querySelectorAll('svg text')).some((t) => {
            const f = t.getAttribute('fill') || ''; return f.replace(/\\s/g, '') === 'rgb(1,2,3)' || f === '#010203';
        })""",
        arg=chart.element_handle(),
    )
    _assert_no_csp_violations(admin_page)

    # The drill-down: a row opens the model's people in place (no page load), the
    # heading takes the focus, and the path leads back.
    expect(admin_page.get_by_test_id("spend-side")).to_contain_text("claude-sonnet-4-5")
    admin_page.evaluate("window.__spDrill = 1")
    rows.filter(has_text="claude-sonnet-4-5").get_by_test_id("spend-open").click()
    admin_page.wait_for_url(re.compile(r"group=user"))
    title = admin_page.get_by_test_id("spend-drill-title")
    expect(title).to_have_text("claude-sonnet-4-5")
    expect(title).to_be_focused()
    assert admin_page.evaluate("window.__spDrill === 1")
    # Person, then their (no) issue, then the sessions.
    admin_page.get_by_test_id("spend-open").first.click()
    admin_page.wait_for_url(re.compile(r"group=issue"))
    admin_page.get_by_test_id("spend-open").first.click()
    admin_page.wait_for_url(re.compile(r"group=sessions"))
    expect(admin_page.get_by_test_id("spend-session")).to_have_count(2)
    admin_page.get_by_test_id("spend-crumb").first.click()
    admin_page.wait_for_url(re.compile(r"group=user"))
    expect(admin_page.get_by_test_id("spend-crumbs")).to_have_count(0)
    admin_page.go_back()
    admin_page.wait_for_url(re.compile(r"group=sessions"))
    expect(admin_page.get_by_test_id("spend-session")).to_have_count(2)

    admin_page.get_by_test_id("spend-entry-list").click()
    admin_page.wait_for_url(f"**/projects/{key}/ai-usage/?**")
    expect(admin_page.locator("tr[data-entry-id]")).to_have_count(2)
    _assert_no_csp_violations(admin_page)
    console.assert_no_errors()


def test_admin_report_presets_and_csv(admin_page: Page, api_client: httpx.Client, project: dict) -> None:
    _log(api_client, project["key"], "claude-sonnet-4-5", "2026-03-03", "1.00", unique_key("Admin report "))
    console = ConsoleErrorTracker().attach(admin_page)

    admin_page.goto("/admin/")
    admin_page.get_by_test_id("admin-ai-spend-card").click()
    admin_page.wait_for_url("**/admin/ai-spend/**")
    admin_page.locator('[data-preset="last_month"]').click()
    expect(admin_page.locator('[data-preset="last_month"]')).to_have_attribute("aria-current", "true")

    admin_page.add_init_script(CSP_WATCH)
    admin_page.goto(f"/admin/ai-spend/?{MARCH}&group=project")
    expect(admin_page.get_by_test_id("spend-table")).to_be_visible()
    chart = admin_page.get_by_test_id("aiu-chart-projects")
    expect(chart).to_have_attribute("data-sp-drawn", "")
    expect(chart.locator("svg text", has_text=project["key"]).first).to_be_visible()
    _assert_no_csp_violations(admin_page)
    expect(admin_page.get_by_test_id("spend-table")).to_contain_text(project["key"])
    href = admin_page.get_by_test_id("spend-csv").get_attribute("href")
    assert href and href.startswith("/api/v1/ai-usage/report/?") and "format=csv" in href
    resp = api_client.get(href)
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/csv")
    assert project["key"] in resp.text
    console.assert_no_errors()
