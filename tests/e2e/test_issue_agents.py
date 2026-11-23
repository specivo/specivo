"""E2E: the Agents tab of an issue, opened from the issue page, and its block at the mobile viewport.

The session is real: a service account calls the REST API with its key, which
opens a session; the session is then put on the issue and given one tool-call
row and one AI usage entry straight in the database, because the browser
suite has no MCP client or reporter.
"""

from __future__ import annotations

import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor

import asyncpg
import httpx
import pytest
from playwright.sync_api import Page, expect

from specivo.testing.e2e_base import create_issue, create_project, unique_key
from tests.e2e.e2e_helpers import ConsoleErrorTracker

pytestmark = [pytest.mark.e2e]


def _db_value(sql: str, *args: object) -> object:
    url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")

    async def _run() -> object:
        conn = await asyncpg.connect(url)
        try:
            return await conn.fetchval(sql, *args)
        finally:
            await conn.close()

    # pytest-playwright runs an event loop of its own; use a fresh one elsewhere.
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, _run()).result()


@pytest.fixture
def agent_issue(api_client: httpx.Client, e2e_server: str) -> dict:
    """An issue that a service account's session worked on, with one tool call."""
    project = create_project(api_client, prefix="IAE")
    key = project["key"]
    issue = create_issue(api_client, key, "Otter migration")
    login = f"iae-agent-{unique_key().lower()}"
    resp = api_client.post(
        "/api/v1/admin/users/",
        json={"login": login, "email": f"{login}@example.com", "display_name": "Otter bot", "is_service_account": True},
    )
    assert resp.status_code == 201, resp.text
    agent_id = resp.json()["id"]
    role_id = _db_value(
        "INSERT INTO roles (name, permissions) VALUES ($1, '[\"view_issues\"]'::jsonb) RETURNING id", f"Agent {key}"
    )
    resp = api_client.post(f"/api/v1/projects/{key}/members/", json={"user_id": agent_id, "role_ids": [role_id]})
    assert resp.status_code in (200, 201), resp.text
    resp = api_client.post(f"/api/v1/admin/users/{agent_id}/api-keys/", json={"name": "otter key"})
    assert resp.status_code == 201, resp.text
    raw_key = resp.json()["raw_key"]

    resp = httpx.get(
        f"{e2e_server}/api/v1/projects/{key}/agent-sessions/",
        headers={"Authorization": f"Bearer {raw_key}", "User-Agent": "otter-agent/1.4"},
        timeout=10,
    )
    assert resp.status_code == 200, resp.text
    session_id = _db_value("SELECT id FROM agent_sessions WHERE user_id = $1", agent_id)
    _db_value(
        "UPDATE agent_sessions SET issue_id = $2, tool_call_count = 1 WHERE id = $1 RETURNING id",
        session_id,
        issue["id"],
    )
    _db_value(
        "INSERT INTO security_audit_logs (event_type, user_id, project_id, agent_session_id, details)"
        " VALUES ('issue_updated', $1, $2, $3, $4::jsonb) RETURNING id",
        agent_id,
        project["id"],
        session_id,
        json.dumps({"tool": "update_issue", "source": "mcp", "issue_ref": issue["key"]}),
    )
    return {"key": key, "issue_key": issue["key"], "session_id": session_id}


def test_agents_tab_shows_the_session(admin_page: Page, agent_issue: dict) -> None:
    tracker = ConsoleErrorTracker().attach(admin_page)
    admin_page.goto(f"/issue/{agent_issue['issue_key']}/")
    tab = admin_page.get_by_test_id("agents-tab-link")
    expect(tab.get_by_test_id("agents-tab-count")).to_have_text("1")
    tab.click()
    block = admin_page.locator(f'[data-testid="agent-block"][data-session-id="{agent_issue["session_id"]}"]')
    expect(block).to_be_visible()
    expect(block).to_contain_text("Otter bot")
    expect(block).to_contain_text("otter-agent 1.4")
    expect(block.get_by_test_id("agent-tool-calls")).to_contain_text("1")
    expect(block.get_by_test_id("as-timeline-row")).to_have_count(1)
    expect(block.get_by_test_id("as-timeline-row")).to_contain_text("update_issue")
    # An administrator sees every entry, so a block without usage says it is not reported yet.
    expect(block.get_by_test_id("agent-cost-pending")).to_be_visible()
    block.get_by_test_id("agent-open").click()
    expect(admin_page.get_by_test_id("as-header")).to_be_visible()
    tracker.assert_no_errors()


@pytest.mark.parametrize("viewport_name", ["mobile"], indirect=True)
def test_block_stacks_on_mobile(responsive_page: Page, viewport_name: str, agent_issue: dict) -> None:
    responsive_page.goto(f"/issue/{agent_issue['issue_key']}/agents/")
    block = responsive_page.get_by_test_id("agent-block")
    expect(block).to_be_visible()
    direction = block.locator(".sp-agent-block-head").evaluate("el => getComputedStyle(el).flexDirection")
    assert direction == "column", viewport_name
    assert responsive_page.evaluate(
        "document.documentElement.scrollWidth <= document.documentElement.clientWidth + 2"
    ), "horizontal overflow at mobile"
