"""Over MCP, an agent session records its client and the issue the agent starts work on.

Each tool is called through the server's own entry point, so the key is
authenticated and the agent session tracked exactly as for a real client. The
client is published the way ``ClientInfoMiddleware`` publishes it.

An issue is recorded when the session's account moves it into an ``active``
status, assigns it to itself, or starts a timer on it; the latest one wins.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import pytest
import pytest_asyncio
from mcp.server.mcpserver.exceptions import ToolError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.mcp import server
from specivo.mcp.auth import mcp_raw_key_var
from specivo.mcp.client_context import agent_session_id_var, mcp_client_var
from specivo.mcp.tools import _update_issue
from specivo.models.agent_session import AgentSession
from specivo.models.issue import Issue
from specivo.models.lookups import IssueStatus
from specivo.models.member import Member, MemberRole
from specivo.models.role import Role
from specivo.models.user import User
from specivo.services.agent_session_service import ClientInfo
from specivo.services.api_key_service import ApiKeyService
from specivo.services.permission_service import clear_role_cache
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
        permissions=["view_issues", "edit_issues", "log_time"],
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
    yield World(agent, peer, key.id, raw_key, backlog, active, done, issue, second, hidden)
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
