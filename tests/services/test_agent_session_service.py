"""AgentSessionService: the client a session records, the issue it works on, and the overlap lookup."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.agent_session import AgentSession
from specivo.models.auth import ApiKey
from specivo.models.issue import Issue
from specivo.models.member import Member, MemberRole
from specivo.models.role import Role
from specivo.models.user import User
from specivo.services.agent_session_service import AgentSessionService, ClientInfo
from specivo.services.api_key_service import ApiKeyService
from specivo.services.permission_service import clear_role_cache
from tests.factories.issue import IssueFactory
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import ServiceAccountFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.service]

_svc = AgentSessionService()
T0 = datetime(2026, 11, 5, 2, 0, tzinfo=UTC)


@dataclass
class World:
    agent: User
    other: User
    key: ApiKey
    other_key: ApiKey
    issue: Issue
    second: Issue
    hidden: Issue


@pytest_asyncio.fixture
async def world(db_session: AsyncSession) -> World:
    status = StatusFactory.build(name="Ass Open")
    db_session.add(status)
    await db_session.flush()
    tracker = TrackerFactory.build(name="Ass Task", default_status_id=status.id)
    priority = PriorityFactory.build(name="Ass Normal")
    member_project = ProjectFactory.build(key="ASSVIS", identifier="assvis", is_public=False)
    hidden_project = ProjectFactory.build(key="ASSHID", identifier="asshid", is_public=False)
    agent = ServiceAccountFactory.build(login="ass_agent", status="active")
    other = ServiceAccountFactory.build(login="ass_other", status="active")
    db_session.add_all([tracker, priority, member_project, hidden_project, agent, other])
    await db_session.flush()

    role = Role(name="Ass Viewer", permissions=["view_issues"], builtin=0, issues_visibility="default")
    db_session.add(role)
    await db_session.flush()
    for user in (agent, other):
        member = Member(project_id=member_project.id, user_id=user.id)
        db_session.add(member)
        await db_session.flush()
        db_session.add(MemberRole(member_id=member.id, role_id=role.id))

    def _issue(project, subject: str) -> Issue:
        return IssueFactory.build(
            project_id=project.id,
            project_key=project.key,
            tracker_id=tracker.id,
            status_id=status.id,
            priority_id=priority.id,
            author_id=agent.id,
            subject=subject,
        )

    issue, second, hidden = (
        _issue(member_project, "Ass first"),
        _issue(member_project, "Ass second"),
        _issue(hidden_project, "Ass hidden"),
    )
    db_session.add_all([issue, second, hidden])
    await db_session.flush()

    keys = ApiKeyService()
    key, _ = await keys.create_key(db_session, user_id=agent.id, name="ass-key")
    other_key, _ = await keys.create_key(db_session, user_id=other.id, name="ass-other-key")
    await db_session.commit()
    clear_role_cache(db_session)
    yield World(agent, other, key, other_key, issue, second, hidden)
    clear_role_cache(db_session)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


async def test_new_session_records_the_mcp_client(db_session: AsyncSession, world: World) -> None:
    s = await _svc.get_or_create_session(
        db_session, world.key.id, world.agent.id, user_agent="node/22", client=ClientInfo("claude-code", "2.1.0")
    )
    assert (s.client_name, s.client_version) == ("claude-code", "2.1.0")


async def test_new_session_falls_back_to_the_user_agent(db_session: AsyncSession, world: World) -> None:
    s = await _svc.get_or_create_session(db_session, world.key.id, world.agent.id, user_agent="codex/0.9 (linux)")
    assert (s.client_name, s.client_version) == ("codex", "0.9")


async def test_session_without_a_client_takes_the_first_one_seen(db_session: AsyncSession, world: World) -> None:
    first = await _svc.get_or_create_session(db_session, world.key.id, world.agent.id)
    assert first.client_name is None

    again = await _svc.get_or_create_session(
        db_session, world.key.id, world.agent.id, client=ClientInfo("claude-code", "2.1.0")
    )
    assert again.id == first.id
    assert (again.client_name, again.client_version) == ("claude-code", "2.1.0")

    third = await _svc.get_or_create_session(
        db_session, world.key.id, world.agent.id, client=ClientInfo("gemini-cli", "1.0")
    )
    assert third.id == first.id
    assert third.client_name == "claude-code"


# ---------------------------------------------------------------------------
# record_issue
# ---------------------------------------------------------------------------


async def test_record_issue_latest_wins(db_session: AsyncSession, world: World) -> None:
    s = await _svc.get_or_create_session(db_session, world.key.id, world.agent.id)

    assert await _svc.record_issue(db_session, s.id, world.agent, world.issue)
    assert s.issue_id == world.issue.id
    assert await _svc.record_issue(db_session, s.id, world.agent, world.second)
    assert s.issue_id == world.second.id


async def test_record_issue_refuses_an_issue_the_account_cannot_see(db_session: AsyncSession, world: World) -> None:
    s = await _svc.get_or_create_session(db_session, world.key.id, world.agent.id)
    await _svc.record_issue(db_session, s.id, world.agent, world.issue)

    assert not await _svc.record_issue(db_session, s.id, world.agent, world.hidden)
    assert s.issue_id == world.issue.id


async def test_record_issue_refuses_another_accounts_session(db_session: AsyncSession, world: World) -> None:
    s = await _svc.get_or_create_session(db_session, world.other_key.id, world.other.id)

    assert not await _svc.record_issue(db_session, s.id, world.agent, world.issue)
    assert s.issue_id is None
    assert not await _svc.record_issue(db_session, s.id + 10_000, world.agent, world.issue)


# ---------------------------------------------------------------------------
# issue_for
# ---------------------------------------------------------------------------


async def _window(
    db: AsyncSession, w: World, start_min: int, end_min: int, issue: Issue | None, *, other: bool = False
) -> AgentSession:
    s = AgentSession(
        api_key_id=(w.other_key if other else w.key).id,
        user_id=(w.other if other else w.agent).id,
        issue_id=issue.id if issue is not None else None,
        started_at=T0 + timedelta(minutes=start_min),
        last_activity_at=T0 + timedelta(minutes=end_min),
    )
    db.add(s)
    await db.flush()
    return s


def _at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


async def test_issue_for_returns_the_overlapping_sessions_issue(db_session: AsyncSession, world: World) -> None:
    await _window(db_session, world, 0, 60, world.issue)

    assert await _svc.issue_for(db_session, world.agent.id, _at(30), _at(90)) == world.issue.id
    # Inside, enclosing, and touching at one instant all overlap.
    assert await _svc.issue_for(db_session, world.agent.id, _at(10), _at(20)) == world.issue.id
    assert await _svc.issue_for(db_session, world.agent.id, _at(-30), _at(120)) == world.issue.id
    assert await _svc.issue_for(db_session, world.agent.id, _at(60), _at(70)) == world.issue.id


async def test_issue_for_ignores_windows_that_do_not_overlap(db_session: AsyncSession, world: World) -> None:
    await _window(db_session, world, 0, 60, world.issue)

    assert await _svc.issue_for(db_session, world.agent.id, _at(61), _at(90)) is None
    assert await _svc.issue_for(db_session, world.agent.id, _at(-30), _at(-1)) is None


async def test_issue_for_ignores_sessions_without_an_issue_and_other_users(
    db_session: AsyncSession, world: World
) -> None:
    await _window(db_session, world, 0, 60, None)
    await _window(db_session, world, 0, 60, world.issue, other=True)

    assert await _svc.issue_for(db_session, world.agent.id, _at(0), _at(60)) is None
    assert await _svc.issue_for(db_session, world.other.id, _at(0), _at(60)) == world.issue.id


async def test_issue_for_prefers_the_longest_overlap(db_session: AsyncSession, world: World) -> None:
    await _window(db_session, world, 0, 50, world.issue)
    await _window(db_session, world, 80, 200, world.second)

    # The record spans 30..100: 20 minutes with the first, 20 with the second.
    # A tie goes to the most recently active session.
    assert await _svc.issue_for(db_session, world.agent.id, _at(30), _at(100)) == world.second.id
    # 10..100: 40 minutes with the first, 20 with the second.
    assert await _svc.issue_for(db_session, world.agent.id, _at(10), _at(100)) == world.issue.id


async def test_issue_for_accepts_a_reversed_window(db_session: AsyncSession, world: World) -> None:
    await _window(db_session, world, 0, 60, world.issue)
    assert await _svc.issue_for(db_session, world.agent.id, _at(50), _at(10)) == world.issue.id
