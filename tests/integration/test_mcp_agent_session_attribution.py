"""Over MCP, an agent session records its client and the issue the agent starts work on.

Each tool is called through the server's own entry point, so the key is
authenticated and the agent session tracked exactly as for a real client. The
client is published the way ``ClientInfoMiddleware`` publishes it.

An issue is recorded when the session's account creates it in an ``active``
status or assigned to itself, moves it into an ``active`` status, assigns it
to itself, updates it while it is ``active`` and its own (resuming it), or
starts a timer on it; the latest one wins.

``specivo_agent_session`` records the client's own session id and its parent
on the calling connection's agent session, and only ever links to sessions of
the same account.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import pytest
import pytest_asyncio
from mcp.server.mcpserver.exceptions import ToolError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import NotFoundError
from specivo.mcp import server
from specivo.mcp.auth import mcp_raw_key_var
from specivo.mcp.client_context import agent_session_id_var, mcp_client_var, mcp_connection_var
from specivo.mcp.tools import _update_issue
from specivo.models.agent_session import AgentSession
from specivo.models.issue import Issue
from specivo.models.lookups import IssueStatus
from specivo.models.member import Member, MemberRole
from specivo.models.role import Role
from specivo.models.security_audit import SecurityAuditLog
from specivo.models.user import User
from specivo.services.agent_session_service import ClientInfo, connection_hash
from specivo.services.api_key_service import ApiKeyService
from specivo.services.permission_service import clear_role_cache
from specivo.services.security_audit_service import AuditEvent
from tests.factories.issue import IssueFactory
from tests.factories.lookups import DoneStatusFactory, PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import ServiceAccountFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.serial]


@dataclass
class World:
    agent: User
    peer: User
    key_id: int
    raw_key: str
    backlog: IssueStatus
    active: IssueStatus
    done: IssueStatus
    issue: Issue
    second: Issue
    hidden: Issue
    tracker_id: int
    priority_id: int


@pytest.fixture
def use_test_session(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Run every tool call on the test's rolled-back session."""

    @asynccontextmanager
    async def _session():
        yield db_session

    monkeypatch.setattr(server, "get_session_factory", lambda: _session)
    key_token = mcp_raw_key_var.set(None)
    client_token = mcp_client_var.set(None)
    yield
    mcp_client_var.reset(client_token)
    mcp_raw_key_var.reset(key_token)


@pytest_asyncio.fixture
async def world(db_session: AsyncSession, use_test_session) -> World:
    backlog = StatusFactory.build(name="Msa Open", category="backlog")
    active = StatusFactory.build(name="Msa Doing", category="active")
    done = DoneStatusFactory.build(name="Msa Done")
    db_session.add_all([backlog, active, done])
    await db_session.flush()
    tracker = TrackerFactory.build(name="Msa Task", default_status_id=backlog.id)
    priority = PriorityFactory.build(name="Msa Normal")
    project = ProjectFactory.build(key="MSAPRJ", identifier="msaprj", is_public=False)
    hidden_project = ProjectFactory.build(key="MSAHID", identifier="msahid", is_public=False)
    agent = ServiceAccountFactory.build(login="msa_agent", status="active")
    peer = UserFactory.build(login="msa_peer", status="active")
    db_session.add_all([tracker, priority, project, hidden_project, agent, peer])
    await db_session.flush()

    role = Role(
        name="Msa Developer",
        permissions=["view_issues", "add_issues", "edit_issues", "log_time"],
        builtin=0,
        issues_visibility="default",
        assignable=True,
    )
    db_session.add(role)
    await db_session.flush()
    for user in (agent, peer):
        member = Member(project_id=project.id, user_id=user.id)
        db_session.add(member)
        await db_session.flush()
        db_session.add(MemberRole(member_id=member.id, role_id=role.id))

    def _issue(p, subject: str) -> Issue:
        return IssueFactory.build(
            project_id=p.id,
            project_key=p.key,
            tracker_id=tracker.id,
            status_id=backlog.id,
            priority_id=priority.id,
            author_id=peer.id,
            subject=subject,
        )

    issue, second, hidden = _issue(project, "Msa first"), _issue(project, "Msa second"), _issue(hidden_project, "x")
    db_session.add_all([issue, second, hidden])
    await db_session.flush()

    key, raw_key = await ApiKeyService().create_key(db_session, user_id=agent.id, name="msa-key")
    await db_session.commit()
    clear_role_cache(db_session)
    mcp_raw_key_var.set(raw_key)
    yield World(agent, peer, key.id, raw_key, backlog, active, done, issue, second, hidden, tracker.id, priority.id)
    clear_role_cache(db_session)


async def _agent_session(db: AsyncSession, w: World) -> AgentSession:
    rows = (await db.execute(select(AgentSession).where(AgentSession.api_key_id == w.key_id))).scalars().all()
    assert len(rows) == 1
    await db.refresh(rows[0])
    return rows[0]


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


async def test_session_records_the_mcp_client(db_session: AsyncSession, world: World) -> None:
    mcp_client_var.set(ClientInfo("claude-code", "2.1.0"))

    await server.specivo_whoami()

    s = await _agent_session(db_session, world)
    assert (s.client_name, s.client_version) == ("claude-code", "2.1.0")
    assert s.issue_id is None
    # The session id is published for the tool call only.
    assert agent_session_id_var.get() is None


async def test_session_without_a_known_client(db_session: AsyncSession, world: World) -> None:
    await server.specivo_whoami()

    s = await _agent_session(db_session, world)
    assert s.client_name is None and s.client_version is None


# ---------------------------------------------------------------------------
# What sets the issue
# ---------------------------------------------------------------------------


async def test_moving_an_issue_into_an_active_status(db_session: AsyncSession, world: World) -> None:
    await server.specivo_update_issue(issue_ref=world.issue.display_key, status_id=world.active.id)

    assert (await _agent_session(db_session, world)).issue_id == world.issue.id


async def test_moving_an_issue_into_another_group_does_not(db_session: AsyncSession, world: World) -> None:
    await server.specivo_update_issue(issue_ref=world.issue.display_key, status_id=world.done.id)

    assert (await _agent_session(db_session, world)).issue_id is None


async def test_editing_an_issue_already_active_does_not(db_session: AsyncSession, world: World) -> None:
    world.second.status_id = world.active.id
    await db_session.commit()

    await server.specivo_update_issue(issue_ref=world.issue.display_key, status_id=world.active.id)
    await server.specivo_update_issue(issue_ref=world.second.display_key, notes="Still on it")

    assert (await _agent_session(db_session, world)).issue_id == world.issue.id


async def test_assigning_an_issue_to_itself(db_session: AsyncSession, world: World) -> None:
    await server.specivo_update_issue(issue_ref=world.issue.display_key, assigned_to_id=world.agent.id)

    assert (await _agent_session(db_session, world)).issue_id == world.issue.id


async def test_assigning_an_issue_to_someone_else_does_not(db_session: AsyncSession, world: World) -> None:
    await server.specivo_update_issue(issue_ref=world.issue.display_key, assigned_to_id=world.peer.id)

    assert (await _agent_session(db_session, world)).issue_id is None


async def test_resuming_an_issue_that_is_active_and_its_own(db_session: AsyncSession, world: World) -> None:
    world.issue.status_id = world.active.id
    world.issue.assigned_to_id = world.agent.id
    await db_session.commit()
    await server.specivo_timer(op="start", issue_ref=world.second.display_key)
    assert (await _agent_session(db_session, world)).issue_id == world.second.id

    # Nothing changes on the issue, yet the agent is back on it.
    await server.specivo_update_issue(issue_ref=world.issue.display_key, notes="Back on it")

    assert (await _agent_session(db_session, world)).issue_id == world.issue.id


async def test_updating_an_own_issue_that_is_not_active_does_not(db_session: AsyncSession, world: World) -> None:
    world.issue.assigned_to_id = world.agent.id
    await db_session.commit()

    await server.specivo_update_issue(issue_ref=world.issue.display_key, notes="Later")

    assert (await _agent_session(db_session, world)).issue_id is None


async def _create(world: World, **kw) -> str:
    return await server.specivo_create_issue(
        project_key="MSAPRJ", tracker_id=world.tracker_id, priority_id=world.priority_id, subject="Msa new", **kw
    )


async def _created(db: AsyncSession, text: str) -> Issue:
    ref = text.split()[2].rstrip(":")
    project_key, number = ref.rsplit("-", 1)
    stmt = select(Issue).where(Issue.project_key == project_key, Issue.sequence_number == int(number))
    return (await db.execute(stmt)).scalar_one()


async def test_creating_an_issue_in_an_active_status(db_session: AsyncSession, world: World) -> None:
    created = await _created(db_session, await _create(world, status_id=world.active.id))

    assert (await _agent_session(db_session, world)).issue_id == created.id


async def test_creating_an_issue_assigned_to_itself(db_session: AsyncSession, world: World) -> None:
    created = await _created(db_session, await _create(world, assigned_to_id=world.agent.id))

    assert created.status_id == world.backlog.id
    assert (await _agent_session(db_session, world)).issue_id == created.id


async def test_creating_an_issue_for_later_or_for_someone_else_does_not(db_session: AsyncSession, world: World) -> None:
    await _create(world)
    await _create(world, assigned_to_id=world.peer.id)

    assert (await _agent_session(db_session, world)).issue_id is None


async def test_starting_a_timer(db_session: AsyncSession, world: World) -> None:
    await server.specivo_timer(op="start", issue_ref=world.issue.display_key)

    assert (await _agent_session(db_session, world)).issue_id == world.issue.id


async def test_the_latest_issue_wins(db_session: AsyncSession, world: World) -> None:
    await server.specivo_timer(op="start", issue_ref=world.issue.display_key)
    await server.specivo_update_issue(issue_ref=world.second.display_key, status_id=world.active.id)
    assert (await _agent_session(db_session, world)).issue_id == world.second.id

    await server.specivo_update_issue(issue_ref=world.issue.display_key, assigned_to_id=world.agent.id)
    assert (await _agent_session(db_session, world)).issue_id == world.issue.id


async def test_an_issue_the_account_cannot_see_is_never_recorded(db_session: AsyncSession, world: World) -> None:
    # Plain values: a refused call rolls back and expires the ORM rows.
    visible_ref, hidden_ref, visible_id, active_id = (
        world.issue.display_key,
        world.hidden.display_key,
        world.issue.id,
        world.active.id,
    )
    await server.specivo_timer(op="start", issue_ref=visible_ref)

    with pytest.raises(ToolError, match="not found"):
        await server.specivo_timer(op="start", issue_ref=hidden_ref)
    with pytest.raises(ToolError, match="not found"):
        await server.specivo_update_issue(issue_ref=hidden_ref, status_id=active_id)

    assert (await _agent_session(db_session, world)).issue_id == visible_id


async def test_outside_a_tracked_tool_call_nothing_is_recorded(db_session: AsyncSession, world: World) -> None:
    # Calling the tool body directly: no agent session is published.
    await _update_issue(db_session, world.agent, world.issue.display_key, status_id=world.active.id)

    rows = (await db_session.execute(select(AgentSession).where(AgentSession.api_key_id == world.key_id))).all()
    assert rows == []


# ---------------------------------------------------------------------------
# specivo_agent_session: the client's own session id, its parent and the issue
# ---------------------------------------------------------------------------


@pytest.fixture
def on_connection() -> Iterator[Callable[[str | None], None]]:
    """Publish the MCP connection the next tool calls come on, as ``ClientInfoMiddleware`` does."""
    token = mcp_connection_var.set(None)

    def _use(connection: str | None) -> None:
        mcp_connection_var.set(connection)

    yield _use
    mcp_connection_var.reset(token)


async def _connection_row(db: AsyncSession, w: World, connection: str) -> AgentSession:
    stmt = select(AgentSession).where(
        AgentSession.api_key_id == w.key_id, AgentSession.connection_hash == connection_hash(connection)
    )
    row = (await db.execute(stmt)).scalar_one()
    await db.refresh(row)
    return row


async def test_set_writes_the_ids_on_the_calling_connection(
    db_session: AsyncSession, world: World, on_connection
) -> None:
    on_connection("conn-a")

    text = await server.specivo_agent_session(op="set", external_session_id="sess_abc")

    row = await _connection_row(db_session, world, "conn-a")
    assert row.external_session_id == "sess_abc"
    assert row.client_type == "mcp"
    assert "sess_abc" in text


async def test_get_returns_the_session(db_session: AsyncSession, world: World, on_connection) -> None:
    on_connection("conn-a")
    mcp_client_var.set(ClientInfo("hermes-agent", "0.4.0"))
    await server.specivo_agent_session(op="set", external_session_id="sess_child", parent_session_id="sess_root")

    text = await server.specivo_agent_session(op="get")

    row = await _connection_row(db_session, world, "conn-a")
    assert f"id: {row.id}" in text
    assert "client: hermes-agent 0.4.0" in text
    assert "client_type: mcp" in text
    assert "external_session_id: sess_child" in text
    assert "parent_external_session_id: sess_root" in text
    assert "issue: (none)" in text


async def test_set_with_an_issue_records_the_working_issue(
    db_session: AsyncSession, world: World, on_connection
) -> None:
    on_connection("conn-a")
    ref, issue_id = world.issue.display_key, world.issue.id

    await server.specivo_agent_session(op="set", external_session_id="sess_abc", issue_ref=ref)

    row = await _connection_row(db_session, world, "conn-a")
    assert row.issue_id == issue_id
    assert ref in await server.specivo_agent_session(op="get")


async def test_set_with_a_hidden_issue_is_refused(db_session: AsyncSession, world: World, on_connection) -> None:
    on_connection("conn-a")
    hidden_ref = world.hidden.display_key
    await server.specivo_agent_session(op="get")

    with pytest.raises(ToolError, match="not found"):
        await server.specivo_agent_session(op="set", external_session_id="sess_abc", issue_ref=hidden_ref)

    row = await _connection_row(db_session, world, "conn-a")
    assert row.issue_id is None
    assert row.external_session_id is None


async def test_set_is_audited_and_setting_the_same_values_again_is_a_no_op(
    db_session: AsyncSession, world: World, on_connection
) -> None:
    on_connection("conn-a")
    ref = world.issue.display_key
    agent_id = world.agent.id

    await server.specivo_agent_session(op="set", external_session_id="sess_abc", parent_session_id="sess_root")
    again = await server.specivo_agent_session(op="set", external_session_id="sess_abc", parent_session_id="sess_root")
    await server.specivo_agent_session(op="set", issue_ref=ref)

    assert "unchanged" in again.lower()
    stmt = (
        select(SecurityAuditLog)
        .where(
            SecurityAuditLog.user_id == agent_id,
            SecurityAuditLog.event_type == str(AuditEvent.AGENT_SESSION_LINKED),
        )
        .order_by(SecurityAuditLog.id)
    )
    logs = (await db_session.execute(stmt)).scalars().all()
    assert [log.details.get("external_session_id") for log in logs] == ["sess_abc", "sess_abc"]
    assert logs[0].details["parent_external_session_id"] == "sess_root"
    assert logs[0].details["tool"] == "agent_session"
    assert logs[1].details["issue"] == ref


async def test_set_refuses_a_bad_id_and_an_unknown_op(world: World, on_connection) -> None:
    on_connection("conn-a")

    with pytest.raises(ToolError, match="external_session_id"):
        await server.specivo_agent_session(op="set", external_session_id="no spaces allowed")
    with pytest.raises(ToolError, match="op"):
        await server.specivo_agent_session(op="delete")
    with pytest.raises(ToolError, match="Nothing to set"):
        await server.specivo_agent_session(op="set")


async def test_a_second_connection_of_the_same_key_creates_no_cross_link(
    db_session: AsyncSession, world: World, on_connection
) -> None:
    on_connection("conn-a")
    await server.specivo_agent_session(op="set", external_session_id="sess_a")
    on_connection("conn-b")
    await server.specivo_agent_session(op="set", external_session_id="sess_b")

    a = await _connection_row(db_session, world, "conn-a")
    b = await _connection_row(db_session, world, "conn-b")
    assert (a.external_session_id, b.external_session_id) == ("sess_a", "sess_b")
    assert a.parent_session_id is None and b.parent_session_id is None


async def test_a_child_connection_links_to_its_parent_connection(
    db_session: AsyncSession, world: World, on_connection
) -> None:
    on_connection("conn-root")
    await server.specivo_agent_session(op="set", external_session_id="sess_root")
    on_connection("conn-child")
    await server.specivo_agent_session(op="set", external_session_id="sess_child", parent_session_id="sess_root")

    root = await _connection_row(db_session, world, "conn-root")
    child = await _connection_row(db_session, world, "conn-child")
    assert child.parent_session_id == root.id


async def test_another_users_external_id_never_becomes_the_parent(
    db_session: AsyncSession, world: World, on_connection
) -> None:
    # The peer's session carries the external id the agent then names as its parent.
    peer_key, _ = await ApiKeyService().create_key(db_session, user_id=world.peer.id, name="msa-peer-key")
    peer_row = AgentSession(
        api_key_id=peer_key.id, user_id=world.peer.id, client_type="mcp", external_session_id="sess_shared"
    )
    db_session.add(peer_row)
    await db_session.commit()
    peer_row_id = peer_row.id

    on_connection("conn-a")
    await server.specivo_agent_session(op="set", external_session_id="sess_shared", parent_session_id="sess_peer")
    on_connection("conn-b")
    await server.specivo_agent_session(op="set", external_session_id="sess_kid", parent_session_id="sess_shared")

    mine_a = await _connection_row(db_session, world, "conn-a")
    mine_b = await _connection_row(db_session, world, "conn-b")
    # The agent's own row with the id is the parent, never the peer's.
    assert mine_b.parent_session_id == mine_a.id
    peer = await db_session.get(AgentSession, peer_row_id)
    assert peer is not None
    await db_session.refresh(peer)
    assert peer.parent_session_id is None
    assert peer.issue_id is None


async def test_without_a_tracked_session_the_tool_says_so(db_session: AsyncSession, world: World) -> None:
    from specivo.mcp.tools.agent_sessions import _agent_session

    with pytest.raises(NotFoundError, match="No agent session"):
        await _agent_session(db_session, world.agent, "get")
