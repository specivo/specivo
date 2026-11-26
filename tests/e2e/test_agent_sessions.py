"""E2E: a project's agent sessions, from the sidebar to one session's tool calls, at the ADR-0005 viewports.

The session is real: a service account calls the REST API with its key, which
opens a session. Its tool-call row is written straight into the audit log,
as ``_log_tool`` would for an MCP call, because the browser suite has no MCP
client.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor

import asyncpg
import httpx
import pytest
from playwright.sync_api import Page, expect

from specivo.testing.e2e_base import create_project, unique_key
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
def agent_project(api_client: httpx.Client, e2e_server: str) -> dict:
    """A project whose service account member opened one REST session with one recorded tool call."""
    project = create_project(api_client, prefix="ASE")
    key = project["key"]
    login = f"ase-agent-{unique_key().lower()}"
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

    # Any request with the key opens (or continues) the account's REST session.
    resp = httpx.get(
        f"{e2e_server}/api/v1/projects/{key}/agent-sessions/",
        headers={"Authorization": f"Bearer {raw_key}", "User-Agent": "otter-agent/1.4"},
        timeout=10,
    )
    assert resp.status_code == 200, resp.text
    session_id = _db_value("SELECT id FROM agent_sessions WHERE user_id = $1", agent_id)
    _db_value(
        "INSERT INTO security_audit_logs (event_type, user_id, project_id, agent_session_id, details)"
        " VALUES ('issue_created', $1, $2, $3, $4::jsonb) RETURNING id",
        agent_id,
        project["id"],
        session_id,
        json.dumps({"tool": "create_issue", "source": "mcp", "project_key": key, "issue_ref": f"{key}-1"}),
    )
    _db_value("UPDATE agent_sessions SET tool_call_count = 1 WHERE id = $1 RETURNING id", session_id)
    return {"key": key, "session_id": session_id}


def _no_horizontal_overflow(page: Page) -> bool:
    return bool(page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth + 2"))


def test_sidebar_list_and_detail(admin_page: Page, agent_project: dict) -> None:
    tracker = ConsoleErrorTracker().attach(admin_page)
    key, session_id = agent_project["key"], agent_project["session_id"]

    admin_page.goto(f"/projects/{key}/issues/")
    admin_page.locator(".sidebar").get_by_role("link", name="Agent sessions").click()
    expect(admin_page).to_have_url(re.compile(rf"/projects/{key}/agent-sessions/$"))
    row = admin_page.locator(f'tr[data-session-id="{session_id}"]')
    expect(row).to_contain_text("Otter bot")
    expect(row).to_contain_text("Service account")
    expect(row).to_contain_text("otter-agent 1.4")

    row.get_by_test_id("as-open").click()
    expect(admin_page.get_by_test_id("as-header")).to_contain_text("REST")
    expect(admin_page.get_by_test_id("as-tool-calls")).to_have_text("1")
    timeline = admin_page.get_by_test_id("as-timeline-row")
    expect(timeline).to_have_count(1)
    expect(timeline).to_contain_text("create_issue")
    expect(timeline).to_contain_text(f"{key}-1")
    tracker.assert_no_errors()


@pytest.mark.parametrize("path", ["", "{id}/"], ids=["list", "detail"])
def test_pages_fit_every_viewport(responsive_page: Page, viewport_name: str, agent_project: dict, path: str) -> None:
    key, session_id = agent_project["key"], agent_project["session_id"]
    responsive_page.goto(f"/projects/{key}/agent-sessions/{path.format(id=session_id)}")
    if path:
        expect(responsive_page.get_by_test_id("as-timeline-row")).to_have_count(1)
    else:
        expect(responsive_page.locator(f'tr[data-session-id="{session_id}"]')).to_be_visible()
        # ADR-0005: the secondary columns go at 768px and below.
        client_header = responsive_page.locator("th", has_text="Client")
        if viewport_name in ("mobile", "tablet"):
            expect(client_header).to_be_hidden()
        else:
            expect(client_header).to_be_visible()
    assert _no_horizontal_overflow(responsive_page), f"horizontal overflow at {viewport_name}"
