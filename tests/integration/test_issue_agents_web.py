"""The Agents tab of an issue: blocks of agent sessions with steps, counters and cost.

- a manager sees the tab with its count on the issue page, and the partial
  shows the block, its subagent, its steps and its cost;
- a lead with ``own`` AI usage visibility sees the block without a cost line;
- a member without ``manage_project`` gets no tab and a 403; a reader who
  cannot see the issue gets a 404 first;
- the no-JS page and the sub-issue switch.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.models.user import User
from specivo.services.auth_service import _make_access_token
from specivo.services.permission_service import clear_role_cache
from tests.integration.agent_timeline_world import TimelineWorld, build_timeline_world

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]


@pytest_asyncio.fixture
async def world(db_session: AsyncSession):
    clear_role_cache(db_session)
    yield await build_timeline_world(db_session, "AIW")
    clear_role_cache(db_session)


def _cookie(user: User) -> dict[str, str]:
    return {"access_token": _make_access_token(user, get_settings())}


async def test_issue_page_offers_the_tab_to_a_manager(client: AsyncClient, world: TimelineWorld) -> None:
    w = world.w
    resp = await client.get(f"/issue/{w.pub.display_key}/", cookies=_cookie(w.manager))
    assert resp.status_code == 200, resp.text
    html = resp.text
    assert 'data-testid="agents-tab-link"' in html
    # The sub-issue's session counts by default: the issue has a sub-issue.
    assert 'data-testid="agents-tab-count">2<' in html
    assert f'hx-get="/partials/issues/{w.pub.display_key}/agents/"' in html


async def test_issue_page_has_no_tab_for_a_member(client: AsyncClient, world: TimelineWorld) -> None:
    w = world.w
    resp = await client.get(f"/issue/{w.pub.display_key}/", cookies=_cookie(w.member))
    assert resp.status_code == 200, resp.text
    assert 'data-testid="agents-tab-link"' not in resp.text
    assert "/agents/" not in resp.text


async def test_partial_shows_the_block(client: AsyncClient, world: TimelineWorld) -> None:
    t, w = world, world.w
    resp = await client.get(
        f"/partials/issues/{w.pub.display_key}/agents/", params={"sub": "0"}, cookies=_cookie(w.manager)
    )
    assert resp.status_code == 200, resp.text
    html = resp.text
    assert html.count('data-testid="agent-block"') == 1
    assert f'data-session-id="{w.old.id}"' in html
    assert "AIW Robot" in html
    assert "Service account" in html
    # The run (MCP and webhook rows) and the subagent are children of the root.
    assert html.count('data-testid="agent-child"') == 2
    assert f'data-session-id="{t.sub.id}"' in html
    assert "Interrupted" in html
    assert "Completed" in html
    # Counters: three tool calls in this project, one approval denied, two subagents.
    assert 'data-testid="agent-tool-calls"><span class="sp-agent-counter-value">3<' in html
    assert "deny 1" in html
    # Steps: this project's rows, the project-free ones and the Hermes ones; never Q's.
    assert html.count('data-testid="as-timeline-row"') == 7
    assert "terminal" in html
    assert "aiw_far_tool" not in html
    assert "show_issue" not in html
    assert "delivery_id" not in html
    # Cost: 1.50 + 0.25, the excluded 9.00 left out; the profile and the unattended badge.
    assert 'data-testid="agent-cost-spend">$1.75<' in html
    assert "night" in html
    assert 'data-testid="aiu-unattended-badge"' in html
    assert f"/projects/{w.p.key}/agent-sessions/{w.old.id}/" in html


async def test_lead_with_own_visibility_sees_no_cost(client: AsyncClient, world: TimelineWorld) -> None:
    t, w = world, world.w
    resp = await client.get(f"/partials/issues/{w.pub.display_key}/agents/", cookies=_cookie(t.dev))
    assert resp.status_code == 200, resp.text
    html = resp.text
    assert html.count('data-testid="agent-block"') == 1
    assert 'data-testid="agent-cost"' not in html
    assert "$1.75" not in html
    # The private sub-issue's session is not shown to the lead.
    assert f'data-session-id="{t.child.id}"' not in html


async def test_member_without_manage_project_gets_403(client: AsyncClient, world: TimelineWorld) -> None:
    w = world.w
    for url in (f"/partials/issues/{w.pub.display_key}/agents/", f"/issue/{w.pub.display_key}/agents/"):
        resp = await client.get(url, cookies=_cookie(w.member))
        assert resp.status_code == 403, url


async def test_hidden_issue_is_404_before_the_permission(client: AsyncClient, world: TimelineWorld) -> None:
    t, w = world, world.w
    resp = await client.get(f"/partials/issues/{w.priv.display_key}/agents/", cookies=_cookie(w.member))
    assert resp.status_code == 404
    resp = await client.get(f"/partials/issues/{t.child_issue.display_key}/agents/", cookies=_cookie(t.dev))
    assert resp.status_code == 404
    resp = await client.get(f"/partials/issues/{w.pub.display_key}/agents/", cookies=_cookie(w.outsider))
    assert resp.status_code == 404


async def test_page_without_javascript_and_the_sub_issue_switch(client: AsyncClient, world: TimelineWorld) -> None:
    t, w = world, world.w
    resp = await client.get(f"/issue/{w.pub.display_key}/agents/", cookies=_cookie(w.manager))
    assert resp.status_code == 200, resp.text
    html = resp.text
    assert 'data-testid="agents-page"' in html
    assert 'data-testid="agents-scope"' in html
    assert html.count('data-testid="agent-block"') == 2
    assert f'data-session-id="{t.child.id}"' in html
    assert f'href="/issue/{w.pub.display_key}/agents/?sub=0"' in html

    resp = await client.get(f"/issue/{w.pub.display_key}/agents/?sub=0", cookies=_cookie(w.manager))
    assert resp.text.count('data-testid="agent-block"') == 1
    assert f'data-session-id="{t.child.id}"' not in resp.text


async def test_issue_without_sessions_shows_the_empty_state(client: AsyncClient, world: TimelineWorld) -> None:
    w = world.w
    resp = await client.get(f"/partials/issues/{w.priv.display_key}/agents/", cookies=_cookie(w.manager))
    assert resp.status_code == 200, resp.text
    # The hidden session is on priv: the manager sees it.
    assert f'data-session-id="{w.hidden.id}"' in resp.text
    resp = await client.get(f"/partials/issues/{w.q_issue.display_key}/agents/", cookies=_cookie(w.admin))
    assert resp.status_code == 200, resp.text
    assert 'data-testid="agents-empty"' in resp.text
