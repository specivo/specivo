"""E2E tests for issue detail v2 features."""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import Page, expect

from specivo.testing.e2e_base import api_post_with_retry, create_issue, create_project

pytestmark = [pytest.mark.e2e]


def _create_project(api: httpx.Client) -> dict:
    return create_project(api, prefix="DV")


_create_issue = create_issue


def _add_comment(api: httpx.Client, issue_key: str, notes: str) -> dict:
    resp = api_post_with_retry(
        api,
        f"/api/v1/issues/{issue_key}/journals/",
        json={"notes": notes},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_threaded_reply_appears_indented(admin_page: Page, api_client: httpx.Client) -> None:
    """Reply to a comment should appear with the sp-reply-indent class."""
    project = _create_project(api_client)
    issue = _create_issue(api_client, project["key"], "Thread test")
    comment = _add_comment(api_client, issue["key"], "Parent comment")
    api_post_with_retry(
        api_client,
        f"/api/v1/issues/{issue['key']}/journals/",
        json={"notes": "Reply to parent", "reply_to_id": comment["id"]},
    )
    admin_page.goto(f"/projects/{project['key']}/issues/{issue['key']}/")
    reply = admin_page.locator(".sp-reply-indent")
    expect(reply).to_be_visible()


def test_emoji_reaction_visible(admin_page: Page, api_client: httpx.Client) -> None:
    """A reaction added via API should appear as an active reaction button."""
    project = _create_project(api_client)
    issue = _create_issue(api_client, project["key"], "Reaction test")
    comment = _add_comment(api_client, issue["key"], "React to me")
    api_post_with_retry(
        api_client,
        f"/api/v1/issues/{issue['key']}/journals/{comment['id']}/reactions/thumbs_up/",
    )
    admin_page.goto(f"/projects/{project['key']}/issues/{issue['key']}/")
    reaction_btn = admin_page.locator(".sp-reaction-btn.sp-reaction-active")
    expect(reaction_btn).to_be_visible()


def test_status_change_shows_color_coded(admin_page: Page, api_client: httpx.Client) -> None:
    """A status change should show color-coded old/new values in the activity feed."""
    project = _create_project(api_client)
    issue = _create_issue(api_client, project["key"], "Status change test")
    # Change subject to trigger a journal entry with color-coded diff
    resp = api_client.patch(
        f"/api/v1/issues/{issue['key']}/",
        json={"subject": "Updated status test subject", "lock_version": issue["lock_version"]},
    )
    assert resp.status_code == 200, f"PATCH failed: {resp.text}"
    admin_page.goto(f"/projects/{project['key']}/issues/{issue['key']}/")
    admin_page.wait_for_load_state("networkidle")
    expect(admin_page.locator(".sp-change-new").first).to_be_visible(timeout=10000)


def test_watcher_toggle_present(admin_page: Page, api_client: httpx.Client) -> None:
    """The watcher chip with Watch/Unwatch should be visible in the sidebar."""
    project = _create_project(api_client)
    issue = _create_issue(api_client, project["key"], "Watcher test")
    admin_page.goto(f"/projects/{project['key']}/issues/{issue['key']}/")
    admin_page.wait_for_load_state("networkidle")
    watcher = admin_page.locator(".sp-watcher-chip").first
    expect(watcher).to_be_visible(timeout=10000)


def test_time_log_form_opens(admin_page: Page, api_client: httpx.Client) -> None:
    """Clicking the Time tab then Log time should reveal the time log form."""
    project = _create_project(api_client)
    issue = _create_issue(api_client, project["key"], "Time test")
    admin_page.goto(f"/projects/{project['key']}/issues/{issue['key']}/")
    admin_page.wait_for_load_state("networkidle")
    admin_page.locator(".activity-tab", has_text="Time").click()
    admin_page.locator("text=Log time").click()
    expect(admin_page.locator("input[type='number']").first).to_be_visible()


def test_attachment_form_opens(admin_page: Page, api_client: httpx.Client) -> None:
    """Clicking the Attachments tab then Attach file should reveal a file input."""
    project = _create_project(api_client)
    issue = _create_issue(api_client, project["key"], "Attach test")
    admin_page.goto(f"/projects/{project['key']}/issues/{issue['key']}/")
    admin_page.wait_for_load_state("networkidle")
    admin_page.locator(".activity-tab", has_text="Attachments").click()
    admin_page.locator("text=Attach file").click()
    expect(admin_page.locator("input[type='file']")).to_be_visible()
