"""One client conversation is one agent session, however its calls arrive.

A Hermes conversation reaches Specivo three ways: MCP tool calls on the
gateway's connection, signed events through the receiver, and REST calls of
the plugin's memory provider. They share the client's session id, and land
in one session:

- events join the open MCP (or REST) session with that id;
- an MCP connection that selects an id the events opened a session for first
  takes that session over (its audit rows and children come along);
- a REST call naming the id (``X-Agent-Session-Id``) joins it too;
- a Hermes run ending does not end a session the client opened over MCP.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.utils import utcnow
from specivo.models.agent_session import AgentSession
from specivo.models.security_audit import SecurityAuditLog
from specivo.services.agent_session_service import AgentSessionService
from specivo.services.hermes_receiver_service import HermesReceiverService
from specivo.services.permission_service import clear_role_cache
from tests.fixtures.hermes_events import payload, post_event
from tests.integration.agent_session_world import SessionWorld, build_session_world

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

_sessions = AgentSessionService()


@pytest_asyncio.fixture
async def world(db_session: AsyncSession):
    clear_role_cache(db_session)
    yield await build_session_world(db_session, "OPC")
    clear_role_cache(db_session)


async def _events_first(client: AsyncClient, db: AsyncSession, world: SessionWorld, external: str) -> int:
    minted = await HermesReceiverService().create(db, owner=world.agent, name="host", actor=world.agent)
    await db.commit()
    for body in (payload("on_session_start", external), payload("post_tool_call", external, tool_name="term_opc")):
        resp = await post_event(client, minted.token, body, minted.secret)
        assert resp.status_code == 200, resp.text
    session_id: int = resp.json()["agent_session_id"]
    row = await db.get(AgentSession, session_id)
    assert row is not None and row.client_type == "webhook"
    return session_id


async def _audit_session_ids(db: AsyncSession) -> set[int | None]:
    rows = await db.execute(
        select(SecurityAuditLog.agent_session_id).where(SecurityAuditLog.event_type == "agent_tool_called")
    )
    return set(rows.scalars().all())


async def test_a_fresh_connection_takes_over_the_session_the_events_opened(
    client: AsyncClient, db_session: AsyncSession, world: SessionWorld
) -> None:
    db = db_session
    webhook_id = await _events_first(client, db, world, "opc-conv")
    fresh = await _sessions.get_or_create_session(
        db, api_key_id=world.live.api_key_id or 0, user_id=world.agent.id, connection="fresh-connection"
    )
    chosen, created = await _sessions.select_for_client(db, fresh.id, world.agent, "opc-conv")
    await _sessions.set_external(db, chosen.id, world.agent, "opc-conv", None)
    await db.commit()

    assert (chosen.id, created) == (fresh.id, False)
    assert await db.get(AgentSession, webhook_id) is None  # folded in
    await db.refresh(chosen)
    assert chosen.tool_call_count == 1 and chosen.receiver_id is not None
    assert await _audit_session_ids(db) == {fresh.id}
    rows = await db.execute(
        select(AgentSession).where(
            AgentSession.user_id == world.agent.id, AgentSession.external_session_id == "opc-conv"
        )
    )
    assert [r.id for r in rows.scalars()] == [fresh.id]


async def test_a_connection_in_another_conversation_adopts_the_events_session(
    client: AsyncClient, db_session: AsyncSession, world: SessionWorld
) -> None:
    db = db_session
    webhook_id = await _events_first(client, db, world, "opc-next")
    # world.live is the connection's current row, already in conversation "opc-live".
    chosen, created = await _sessions.select_for_client(db, world.live.id, world.agent, "opc-next")
    await db.commit()
    assert (chosen.id, created) == (webhook_id, False)
    await db.refresh(chosen)
    assert (chosen.api_key_id, chosen.connection_hash, chosen.client_type) == (
        world.live.api_key_id,
        world.live.connection_hash,
        "mcp",
    )


async def test_a_rest_call_naming_the_conversation_joins_its_session(
    client: AsyncClient, db_session: AsyncSession, world: SessionWorld
) -> None:
    db = db_session
    headers = {"Authorization": f"Bearer {world.agent_raw_key}", "X-Agent-Session-Id": "opc-live"}
    before = world.live.last_activity_at
    resp = await client.get("/api/v1/projects/", headers=headers)
    assert resp.status_code == 200, resp.text
    await db.refresh(world.live)
    assert world.live.last_activity_at > before

    # Without the header a REST call keeps to the key's plain REST session, never the named one.
    plain = await client.get("/api/v1/projects/", headers={"Authorization": f"Bearer {world.agent_raw_key}"})
    assert plain.status_code == 200
    rest_rows = await db.execute(
        select(AgentSession).where(
            AgentSession.user_id == world.agent.id,
            AgentSession.client_type == "rest",
            AgentSession.last_activity_at > utcnow() - timedelta(minutes=1),
        )
    )
    assert [r.external_session_id for r in rest_rows.scalars()] == [None]

    # A header naming no open session starts a REST session carrying the id; a bad id is ignored.
    await client.get("/api/v1/projects/", headers={**headers, "X-Agent-Session-Id": "opc-memory"})
    rows = await db.execute(select(AgentSession).where(AgentSession.external_session_id == "opc-memory"))
    [row] = rows.scalars().all()
    assert row.client_type == "rest" and row.user_id == world.agent.id
    bad = await client.get("/api/v1/projects/", headers={**headers, "X-Agent-Session-Id": "../etc"})
    assert bad.status_code == 200


async def test_an_mcp_call_in_a_project_names_it_on_its_audit_row(
    db_session: AsyncSession, world: SessionWorld
) -> None:
    """list_issues and the like log a project key; the row carries the project, so its timeline shows it."""
    from specivo.mcp.tools._common import _log_tool
    from specivo.services.security_audit_service import AuditEvent

    await _log_tool(
        db_session, world.agent, AuditEvent.ISSUE_LISTED, "list_issues", {"project_key": world.p.key.lower()}
    )
    await _log_tool(db_session, world.agent, AuditEvent.ISSUE_LISTED, "list_issues", {"project_key": "NOSUCH"})
    await db_session.flush()
    rows = (
        await db_session.execute(
            select(SecurityAuditLog.project_id, SecurityAuditLog.details["project_key"].astext)
            .where(SecurityAuditLog.event_type == "issue_listed", SecurityAuditLog.user_id == world.agent.id)
            .order_by(SecurityAuditLog.id)
        )
    ).all()
    assert [r[0] for r in rows] == [world.p.id, None]


async def test_a_background_rest_call_opens_and_extends_no_session(
    client: AsyncClient, db_session: AsyncSession, world: SessionWorld
) -> None:
    """A client's own housekeeping (X-Agent-Background: 1, e.g. a sync poll) is no agent work."""
    db = db_session
    before = world.live.last_activity_at
    count = await db.scalar(
        select(func.count()).select_from(AgentSession).where(AgentSession.user_id == world.agent.id)
    )
    headers = {
        "Authorization": f"Bearer {world.agent_raw_key}",
        "X-Agent-Background": "1",
        "X-Agent-Session-Id": "opc-live",
    }
    resp = await client.get(f"/api/v1/projects/{world.p.key}/issues/", params={"assigned_to_id": "me"}, headers=headers)
    assert resp.status_code == 200, resp.text
    await db.refresh(world.live)
    assert world.live.last_activity_at == before
    after = await db.scalar(
        select(func.count()).select_from(AgentSession).where(AgentSession.user_id == world.agent.id)
    )
    assert after == count
