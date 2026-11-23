"""The project's agent session pages: the list, a session's detail and its tool-call timeline.

- the list renders with its sidebar link, filters and usage, and leaves out a
  session on an issue the reader cannot see;
- the detail shows the header, the timeline (this project's rows and the
  project-less ones) and the linked AI usage, and records the view;
- detail and timeline need manage_project: 403 for a member, 404 for a session
  that is not the project's.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.models.security_audit import SecurityAuditLog
from specivo.models.user import User
from specivo.services.auth_service import _make_access_token
from specivo.services.permission_service import clear_role_cache
from tests.integration.agent_session_world import SessionWorld, build_session_world

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]


@pytest_asyncio.fixture
async def world(db_session: AsyncSession):
    clear_role_cache(db_session)
    yield await build_session_world(db_session, "ASW")
    clear_role_cache(db_session)


def _cookie(user: User) -> dict[str, str]:
    return {"access_token": _make_access_token(user, get_settings())}


async def test_list_renders_sessions_with_sidebar_link(client: AsyncClient, world: SessionWorld) -> None:
    w = world
    resp = await client.get(f"/projects/{w.p.key}/agent-sessions/", cookies=_cookie(w.manager))
    assert resp.status_code == 200, resp.text
    html = resp.text
    assert f'href="/projects/{w.p.key}/agent-sessions/"' in html
    assert 'data-testid="as-table"' in html
    for s in (w.live, w.hidden, w.old):
        assert f'data-session-id="{s.id}"' in html
    assert "ASW Robot" in html
    assert "Service account" in html
    assert "claude-code 2.1.0" in html
    assert w.pub.display_key in html
    # Managers get a link to each session's detail.
    assert f"/projects/{w.p.key}/agent-sessions/{w.live.id}/" in html
    assert "$1.50" in html


async def test_list_omits_the_hidden_issue_session_and_links_for_a_member(
    client: AsyncClient, world: SessionWorld
) -> None:
    w = world
    resp = await client.get(f"/projects/{w.p.key}/agent-sessions/", cookies=_cookie(w.member))
    assert resp.status_code == 200, resp.text
    html = resp.text
    assert f'data-session-id="{w.live.id}"' in html
    assert f'data-session-id="{w.hidden.id}"' not in html
    assert w.priv.display_key not in html
    assert 'data-testid="as-open"' not in html


async def test_list_filters(client: AsyncClient, world: SessionWorld) -> None:
    w = world
    url = f"/projects/{w.p.key}/agent-sessions/"
    resp = await client.get(url, params={"active": "1"}, cookies=_cookie(w.manager))
    assert f'data-session-id="{w.live.id}"' in resp.text
    assert f'data-session-id="{w.old.id}"' not in resp.text

    resp = await client.get(url, params={"issue": "ZZNOPE-3"}, cookies=_cookie(w.manager))
    assert 'data-testid="as-empty"' in resp.text
    assert "No agent sessions match these filters." in resp.text


async def test_list_is_404_for_an_outsider(client: AsyncClient, world: SessionWorld) -> None:
    resp = await client.get(f"/projects/{world.p.key}/agent-sessions/", cookies=_cookie(world.outsider))
    assert resp.status_code == 404


async def test_detail_shows_header_timeline_and_usage(
    client: AsyncClient, db_session: AsyncSession, world: SessionWorld
) -> None:
    w = world
    resp = await client.get(f"/projects/{w.p.key}/agent-sessions/{w.live.id}/", cookies=_cookie(w.manager))
    assert resp.status_code == 200, resp.text
    html = resp.text
    assert 'data-testid="as-header"' in html
    assert "asw-live" in html
    assert "asw-root" in html
    assert f'href="/projects/{w.p.key}/agent-sessions/{w.old.id}/" data-testid="as-parent-link"' in html
    assert f'href="/issue/{w.pub.display_key}/"' in html
    # Timeline: this project's row and the project-less one, not the other project's.
    assert html.count('data-testid="as-timeline-row"') == 2
    assert "create_issue_asw" in html
    assert "whoami" in html
    assert "search_asw" not in html
    # Linked AI usage: both entries of the client session id, the excluded one marked.
    assert html.count("data-entry-id=") == 2

    viewed = (
        (
            await db_session.execute(
                select(SecurityAuditLog).where(
                    SecurityAuditLog.resource_type == "agent_session", SecurityAuditLog.resource_id == w.live.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(viewed) == 1


async def test_timeline_partial_renders_alone(client: AsyncClient, world: SessionWorld) -> None:
    w = world
    resp = await client.get(f"/projects/{w.p.key}/agent-sessions/{w.live.id}/timeline/", cookies=_cookie(w.manager))
    assert resp.status_code == 200, resp.text
    assert resp.text.lstrip().startswith('<div class="card sp-as-timeline"')
    assert resp.text.count('data-testid="as-timeline-row"') == 2


async def test_detail_and_timeline_need_manage_project(client: AsyncClient, world: SessionWorld) -> None:
    w = world
    for suffix in ("", "timeline/"):
        resp = await client.get(f"/projects/{w.p.key}/agent-sessions/{w.live.id}/{suffix}", cookies=_cookie(w.member))
        assert resp.status_code == 403


async def test_detail_of_a_session_outside_the_project_is_404(client: AsyncClient, world: SessionWorld) -> None:
    w = world
    resp = await client.get(f"/projects/{w.p.key}/agent-sessions/987654321/", cookies=_cookie(w.manager))
    assert resp.status_code == 404


async def test_signed_out_reader_is_sent_to_login(client: AsyncClient, world: SessionWorld) -> None:
    resp = await client.get(f"/projects/{world.p.key}/agent-sessions/")
    assert resp.status_code in (302, 303)
    assert "/login/" in resp.headers["location"]
