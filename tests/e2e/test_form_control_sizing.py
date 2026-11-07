"""E2E: inputs, selects and buttons of an inline form row have one height, and select arrows sit inside the border.

Also the issue picker of the "Move the selected entries to" form on My AI
usage: it suggests issues while you type, follows the project select, and
fills the key in without submitting the form.
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import Page, expect

from specivo.testing.e2e_base import create_issue, create_project, unique_key
from tests.e2e.e2e_helpers import ConsoleErrorTracker

pytestmark = [pytest.mark.e2e]

# Visible controls of every form in the page content, grouped into rows by
# their vertical centre; a row whose heights differ by more than 1px is
# reported. Selects report their right padding and arrow.
_MEASURE = r"""
() => {
  const pick = 'input:not([type=hidden]):not([type=checkbox]):not([type=radio]), '
    + 'select, button, a.sp-btn, a.sp-btn-sm, .sp-filter-chip';
  const mismatched = [];
  for (const form of document.querySelectorAll('.main-content form, main form')) {
    const rows = [];
    for (const el of form.querySelectorAll(pick)) {
      const r = el.getBoundingClientRect();
      if (!r.width || !r.height || el.closest('.sp-ac-menu, .sp-modal, .lightbox, .view-toggle')) continue;
      const c = r.top + r.height / 2;
      let row = rows.find(x => Math.abs(x.c - c) < 6);
      if (!row) { row = { c, items: [] }; rows.push(row); }
      row.items.push({ el: (el.innerText || el.name || el.tagName).trim().slice(0, 30), h: r.height });
    }
    for (const row of rows) {
      if (row.items.length < 2) continue;
      const hs = row.items.map(i => i.h);
      if (Math.max(...hs) - Math.min(...hs) > 1) mismatched.push(row.items);
    }
  }
  const selects = [...document.querySelectorAll('select:not([multiple])')]
    .filter(s => s.getBoundingClientRect().width)
    .map(s => {
      const cs = getComputedStyle(s);
      return { name: s.name, padRight: parseFloat(cs.paddingRight), appearance: cs.appearance,
               arrow: cs.backgroundImage.includes('svg') };
    });
  return { mismatched, selects };
}
"""


def _check(page: Page, path: str, min_rows_checked: int = 1) -> None:
    page.goto(path)
    page.wait_for_load_state("networkidle")
    result = page.evaluate(_MEASURE)
    assert result["mismatched"] == [], f"{path}: controls of one row differ in height: {result['mismatched']}"
    assert len(result["selects"]) >= min_rows_checked, f"{path}: no select measured"
    for sel in result["selects"]:
        assert sel["padRight"] >= 8, f"{path}: select {sel['name']} keeps only {sel['padRight']}px for its arrow"
        assert sel["appearance"] == "none" and sel["arrow"], f"{path}: select {sel['name']} uses the native arrow"


@pytest.fixture
def wide(admin_page: Page) -> Page:
    admin_page.set_viewport_size({"width": 1440, "height": 900})
    return admin_page


@pytest.mark.parametrize(
    "path",
    ["/admin/ai-model-prices/", "/admin/exchange-rates/", "/admin/ai-subscriptions/", "/admin/currencies/"],
)
def test_admin_form_rows_line_up(wide: Page, path: str) -> None:
    _check(wide, path)


def test_issue_filter_bar_lines_up(wide: Page, api_client: httpx.Client) -> None:
    project = create_project(api_client, prefix="FCS")
    create_issue(api_client, project["key"], "Line up the filter bar")
    _check(wide, f"/projects/{project['key']}/issues/")


def _log_unassigned(api: httpx.Client, comments: str) -> dict:
    body = {
        "tool": "claude-code",
        "model": "claude-sonnet-4-5",
        "usage_date": "2026-03-10",
        "tokens": {"input": 1000, "output": 100},
        "cost_type": "api",
        "comments": comments,
    }
    resp = api.post("/api/v1/my/ai-usage/", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_move_form_lines_up_and_picks_an_issue(wide: Page, api_client: httpx.Client) -> None:
    project = create_project(api_client, prefix="FCM")
    other = create_project(api_client, prefix="FCO")
    subject = unique_key("Tidy the move form ")
    issue = create_issue(api_client, project["key"], subject)
    create_issue(api_client, other["key"], subject + " elsewhere")
    entry = _log_unassigned(api_client, unique_key("Move me "))
    console = ConsoleErrorTracker().attach(wide)

    _check(wide, "/my/ai-usage/?project=-")
    form = wide.get_by_test_id("aiu-reassign-form")
    # The project select is a normal white field, not greyed out like a disabled one.
    bg = wide.get_by_test_id("aiu-reassign-project").evaluate("el => getComputedStyle(el).backgroundColor")
    assert bg == "rgb(255, 255, 255)", bg

    # Scoped to the chosen project: only its issue is suggested.
    wide.get_by_test_id("aiu-reassign-project").select_option(project["key"])
    field = wide.get_by_test_id("aiu-reassign-issue")
    field.click()
    field.press_sequentially(subject[-8:], delay=30)
    options = form.locator('[role="option"]')
    expect(options).to_have_count(1)
    expect(options.first).to_contain_text(issue["key"])
    options.first.click()
    expect(field).to_have_value(issue["key"])
    expect(form.locator('[role="listbox"]')).to_be_hidden()
    assert "/my/ai-usage/" in wide.url and "notice" not in wide.url  # picking did not submit

    wide.locator(f'tr[data-entry-id="{entry["id"]}"] input[name="entry_ids"]').check()
    wide.get_by_test_id("aiu-reassign-submit").click()
    expect(wide.get_by_test_id("aiu-notice")).to_be_visible()
    moved = api_client.get(f"/api/v1/projects/{project['key']}/ai-usage/").json()["items"]
    assert [e["issue_key"] for e in moved if e["id"] == entry["id"]] == [issue["key"]]
    console.assert_no_errors()
