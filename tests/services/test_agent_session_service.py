"""AgentSessionService: the client a session records, the issue it works on, the overlap lookup and external ids."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import NotFoundError, ValidationError
from specivo.models.agent_session import AgentSession
from specivo.models.auth import ApiKey
from specivo.models.issue import Issue
from specivo.models.member import Member, MemberRole
from specivo.models.role import Role
from specivo.models.user import User
from specivo.services.agent_session_service import (
    CLIENT_TYPE_MCP,
    CLIENT_TYPE_REST,
    CLIENT_TYPE_WEBHOOK,
    CLIENT_TYPES,
    LINEAGE_MAX_DEPTH,
    AgentSessionService,
    ClientInfo,
)
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
# Connections: one agent session per MCP connection
# ---------------------------------------------------------------------------


async def test_each_connection_of_a_key_gets_its_own_session(db_session: AsyncSession, world: World) -> None:
    first = await _svc.get_or_create_session(db_session, world.key.id, world.agent.id, connection="conn-a")
    await _svc.record_issue(db_session, first.id, world.agent, world.issue)

    # A new connection of the same key, well inside the idle timeout.
    second = await _svc.get_or_create_session(db_session, world.key.id, world.agent.id, connection="conn-b")

    assert second.id != first.id
    assert second.issue_id is None
    assert first.issue_id == world.issue.id


async def test_the_same_connection_keeps_its_session(db_session: AsyncSession, world: World) -> None:
    first = await _svc.get_or_create_session(db_session, world.key.id, world.agent.id, connection="conn-a")
    await _svc.get_or_create_session(db_session, world.key.id, world.agent.id, connection="conn-b")

    again = await _svc.get_or_create_session(db_session, world.key.id, world.agent.id, connection="conn-a")
    assert again.id == first.id


async def test_the_connection_is_stored_hashed(db_session: AsyncSession, world: World) -> None:
    s = await _svc.get_or_create_session(db_session, world.key.id, world.agent.id, connection="conn-secret")
    assert s.connection_hash is not None
    assert len(s.connection_hash) == 64
    assert "conn-secret" not in s.connection_hash


async def test_an_idle_connection_starts_a_new_session(db_session: AsyncSession, world: World) -> None:
    first = await _svc.get_or_create_session(db_session, world.key.id, world.agent.id, connection="conn-a")
    first.last_activity_at = datetime.now(UTC) - timedelta(hours=2)
    await db_session.flush()

    again = await _svc.get_or_create_session(db_session, world.key.id, world.agent.id, connection="conn-a")
    assert again.id != first.id


async def test_calls_without_a_connection_share_the_keys_session(db_session: AsyncSession, world: World) -> None:
    mcp = await _svc.get_or_create_session(db_session, world.key.id, world.agent.id, connection="conn-a")

    rest = await _svc.get_or_create_session(db_session, world.key.id, world.agent.id, user_agent="curl/8")
    rest_again = await _svc.get_or_create_session(db_session, world.key.id, world.agent.id)

    # REST calls neither join nor extend a connection's session; they keep one of their own.
    assert rest.id != mcp.id
    assert rest_again.id == rest.id
    assert rest.connection_hash is None


async def test_a_connection_never_joins_another_keys_session(db_session: AsyncSession, world: World) -> None:
    mine = await _svc.get_or_create_session(db_session, world.key.id, world.agent.id, connection="conn-a")
    theirs = await _svc.get_or_create_session(db_session, world.other_key.id, world.other.id, connection="conn-a")
    assert theirs.id != mine.id


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


# ---------------------------------------------------------------------------
# Client type
# ---------------------------------------------------------------------------


async def test_client_type_follows_how_the_session_was_opened(db_session: AsyncSession, world: World) -> None:
    mcp = await _svc.get_or_create_session(db_session, world.key.id, world.agent.id, connection="conn-a")
    rest = await _svc.get_or_create_session(db_session, world.key.id, world.agent.id)
    in_process = await _svc.get_or_create_session(
        db_session, world.other_key.id, world.other.id, client_type=CLIENT_TYPE_MCP
    )

    assert mcp.client_type == CLIENT_TYPE_MCP
    assert rest.client_type == CLIENT_TYPE_REST
    assert in_process.client_type == CLIENT_TYPE_MCP
    assert {CLIENT_TYPE_MCP, CLIENT_TYPE_REST, CLIENT_TYPE_WEBHOOK} == set(CLIENT_TYPES)


async def test_client_type_outside_the_known_values_is_refused_by_the_database(
    db_session: AsyncSession, world: World
) -> None:
    row = AgentSession(api_key_id=world.key.id, user_id=world.agent.id, client_type="carrier-pigeon")
    savepoint = await db_session.begin_nested()
    db_session.add(row)
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await savepoint.rollback()


async def test_only_webhook_sessions_may_have_no_api_key(db_session: AsyncSession, world: World) -> None:
    webhook = AgentSession(api_key_id=None, user_id=world.agent.id, client_type=CLIENT_TYPE_WEBHOOK)
    db_session.add(webhook)
    await db_session.flush()
    assert webhook.id is not None

    savepoint = await db_session.begin_nested()
    db_session.add(AgentSession(api_key_id=None, user_id=world.agent.id, client_type=CLIENT_TYPE_MCP))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await savepoint.rollback()


# ---------------------------------------------------------------------------
# External session id
# ---------------------------------------------------------------------------


async def _conn(db: AsyncSession, w: World, connection: str, *, other: bool = False) -> AgentSession:
    return await _svc.get_or_create_session(
        db,
        (w.other_key if other else w.key).id,
        (w.other if other else w.agent).id,
        connection=connection,
    )


async def test_set_external_records_the_ids(db_session: AsyncSession, world: World) -> None:
    s = await _conn(db_session, world, "conn-a")

    changed = await _svc.set_external(db_session, s.id, world.agent, "sess_abc", None)

    assert changed
    assert s.external_session_id == "sess_abc"
    assert s.parent_external_session_id is None
    assert s.parent_session_id is None
    # The same values again change nothing.
    assert not await _svc.set_external(db_session, s.id, world.agent, "sess_abc", None)


@pytest.mark.parametrize("bad", ["", " sess", "-leading-dash", "has space", "x" * 201, "sess/abc", "ok\n"])
async def test_set_external_refuses_ids_outside_the_pattern(db_session: AsyncSession, world: World, bad: str) -> None:
    s = await _conn(db_session, world, "conn-a")

    with pytest.raises(ValidationError):
        await _svc.set_external(db_session, s.id, world.agent, bad, None)
    with pytest.raises(ValidationError):
        await _svc.set_external(db_session, s.id, world.agent, "sess_ok", bad)
    assert s.external_session_id is None


async def test_set_external_accepts_the_pattern_characters(db_session: AsyncSession, world: World) -> None:
    s = await _conn(db_session, world, "conn-a")
    value = "20261123_142635_ab12:cd-34.e" + "x" * 150
    await _svc.set_external(db_session, s.id, world.agent, value, None)
    assert s.external_session_id == value


async def test_a_session_cannot_be_its_own_parent(db_session: AsyncSession, world: World) -> None:
    s = await _conn(db_session, world, "conn-a")
    with pytest.raises(ValidationError):
        await _svc.set_external(db_session, s.id, world.agent, "sess_abc", "sess_abc")


async def test_set_external_refuses_another_accounts_session(db_session: AsyncSession, world: World) -> None:
    theirs = await _conn(db_session, world, "conn-a", other=True)

    with pytest.raises(NotFoundError):
        await _svc.set_external(db_session, theirs.id, world.agent, "sess_abc", None)
    assert theirs.external_session_id is None


async def test_find_by_external_lists_the_users_rows_newest_first(db_session: AsyncSession, world: World) -> None:
    first = await _conn(db_session, world, "conn-a")
    await _svc.set_external(db_session, first.id, world.agent, "sess_abc", None)
    first.last_activity_at = datetime.now(UTC) - timedelta(minutes=5)
    # A reconnect: another connection row with the same external id.
    second = await _conn(db_session, world, "conn-b")
    await _svc.set_external(db_session, second.id, world.agent, "sess_abc", None)
    theirs = await _conn(db_session, world, "conn-c", other=True)
    await _svc.set_external(db_session, theirs.id, world.other, "sess_abc", None)

    found = await _svc.find_by_external(db_session, world.agent.id, "sess_abc")

    assert [s.id for s in found] == [second.id, first.id]
    assert await _svc.find_by_external(db_session, world.agent.id, "sess_none") == []
    assert [s.id for s in await _svc.find_by_external(db_session, world.other.id, "sess_abc")] == [theirs.id]


async def test_parent_reported_before_the_child_is_linked(db_session: AsyncSession, world: World) -> None:
    parent = await _conn(db_session, world, "conn-parent")
    await _svc.set_external(db_session, parent.id, world.agent, "sess_root", None)
    child = await _conn(db_session, world, "conn-child")

    await _svc.set_external(db_session, child.id, world.agent, "sess_child", "sess_root")

    assert child.parent_external_session_id == "sess_root"
    assert child.parent_session_id == parent.id


async def test_parent_reported_after_the_child_is_back_filled(db_session: AsyncSession, world: World) -> None:
    child = await _conn(db_session, world, "conn-child")
    await _svc.set_external(db_session, child.id, world.agent, "sess_child", "sess_root")
    assert child.parent_session_id is None

    parent = await _conn(db_session, world, "conn-parent")
    await _svc.set_external(db_session, parent.id, world.agent, "sess_root", None)

    await db_session.refresh(child)
    assert child.parent_session_id == parent.id


async def test_another_users_session_is_never_a_parent(db_session: AsyncSession, world: World) -> None:
    theirs = await _conn(db_session, world, "conn-theirs", other=True)
    await _svc.set_external(db_session, theirs.id, world.other, "sess_root", None)
    child = await _conn(db_session, world, "conn-child")

    await _svc.set_external(db_session, child.id, world.agent, "sess_child", "sess_root")
    assert child.parent_session_id is None

    # Nor is a child of another user back-filled when this user reports the parent id.
    their_child = await _conn(db_session, world, "conn-their-child", other=True)
    await _svc.set_external(db_session, their_child.id, world.other, "sess_kid", "sess_mine")
    mine = await _conn(db_session, world, "conn-mine")
    await _svc.set_external(db_session, mine.id, world.agent, "sess_mine", None)
    await db_session.refresh(their_child)
    assert their_child.parent_session_id is None


async def _chain(db: AsyncSession, w: World, length: int) -> list[AgentSession]:
    rows: list[AgentSession] = []
    for n in range(length):
        row = await _conn(db, w, f"conn-chain-{n}")
        await _svc.set_external(db, row.id, w.agent, f"sess_{n}", f"sess_{n - 1}" if n else None)
        rows.append(row)
    return rows


async def test_lineage_walks_ancestors_and_descendants(db_session: AsyncSession, world: World) -> None:
    rows = await _chain(db_session, world, 4)
    sibling = await _conn(db_session, world, "conn-sibling")
    await _svc.set_external(db_session, sibling.id, world.agent, "sess_sibling", "sess_1")

    lineage = await _svc.lineage(db_session, rows[1])

    ids = [s.id for s in lineage]
    # Ancestors nearest first, then descendants.
    assert ids[0] == rows[0].id
    assert set(ids) == {rows[0].id, rows[2].id, rows[3].id, sibling.id}
    assert rows[1].id not in ids


async def test_lineage_is_bounded_in_depth(db_session: AsyncSession, world: World) -> None:
    rows = await _chain(db_session, world, 15)

    up = await _svc.lineage(db_session, rows[-1])
    down = await _svc.lineage(db_session, rows[0])

    assert len(up) == LINEAGE_MAX_DEPTH
    assert len(down) == LINEAGE_MAX_DEPTH


async def test_lineage_survives_a_cycle(db_session: AsyncSession, world: World) -> None:
    a, b = await _chain(db_session, world, 2)
    # Forge a cycle directly: a's parent is b, b's parent is a.
    a.parent_session_id = b.id
    await db_session.flush()

    lineage = await _svc.lineage(db_session, a)

    assert [s.id for s in lineage] == [b.id]


async def test_issue_for_external_uses_the_newest_row_then_the_lineage(db_session: AsyncSession, world: World) -> None:
    root, child = await _chain(db_session, world, 2)
    assert await _svc.issue_for_external(db_session, world.agent.id, "sess_1") is None

    await _svc.record_issue(db_session, root.id, world.agent, world.issue)
    # The child names no issue: its parent's is used.
    assert await _svc.issue_for_external(db_session, world.agent.id, "sess_1") == world.issue.id

    await _svc.record_issue(db_session, child.id, world.agent, world.second)
    assert await _svc.issue_for_external(db_session, world.agent.id, "sess_1") == world.second.id
    assert await _svc.issue_for_external(db_session, world.agent.id, "sess_0") == world.issue.id
    # Another user's rows never answer.
    assert await _svc.issue_for_external(db_session, world.other.id, "sess_1") is None
    assert await _svc.issue_for_external(db_session, world.agent.id, "sess_unknown") is None


# ---------------------------------------------------------------------------
# A project's session list: filters, counts and the usage of each session
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def sessions_world(db_session: AsyncSession):
    from tests.integration.agent_session_world import build_session_world

    clear_role_cache(db_session)
    yield await build_session_world(db_session, "ASS")
    clear_role_cache(db_session)


async def test_list_for_project_counts_and_filters(db_session: AsyncSession, sessions_world) -> None:
    from specivo.services.agent_session_service import AgentSessionFilters

    w = sessions_world
    items, total = await _svc.list_for_project(db_session, w.p.id, w.manager)
    assert total == 3
    assert [s.id for s in items] == [w.live.id, w.hidden.id, w.old.id]

    items, total = await _svc.list_for_project(db_session, w.p.id, w.manager, offset=1, limit=1)
    assert (total, [s.id for s in items]) == (3, [w.hidden.id])

    items, total = await _svc.list_for_project(db_session, w.p.id, w.manager, AgentSessionFilters(active=True))
    assert (total, [s.id for s in items]) == (1, [w.live.id])

    items, total = await _svc.list_for_project(db_session, w.p.id, w.manager, AgentSessionFilters(issue_id=w.priv.id))
    assert [s.id for s in items] == [w.hidden.id]

    # The member cannot see the private issue: its session is not counted either.
    items, total = await _svc.list_for_project(db_session, w.p.id, w.member)
    assert total == 2
    assert w.hidden.id not in {s.id for s in items}


async def test_filters_for_resolves_or_matches_nothing(db_session: AsyncSession, sessions_world) -> None:
    w = sessions_world
    filters = await _svc.filters_for(db_session, w.p.id, w.manager, account=w.agent.login, issue_ref=w.pub.display_key)
    assert filters is not None
    assert (filters.user_id, filters.issue_id) == (w.agent.id, w.pub.id)
    assert await _svc.filters_for(db_session, w.p.id, w.manager, account="no-such-login") is None
    assert await _svc.filters_for(db_session, w.p.id, w.member, issue_ref=w.priv.display_key) is None


async def test_get_for_project_hides_other_projects_and_hidden_issues(db_session: AsyncSession, sessions_world) -> None:
    w = sessions_world
    assert (await _svc.get_for_project(db_session, w.p.id, w.live.id, w.manager)).id == w.live.id
    with pytest.raises(NotFoundError):
        await _svc.get_for_project(db_session, w.p.id, w.hidden.id, w.member)
    # The agent is not a member of the manager's other projects: none of its sessions are theirs.
    with pytest.raises(NotFoundError):
        await _svc.get_for_project(db_session, w.q.id, w.hidden.id, w.manager)


async def test_is_active_needs_recent_activity_and_no_end(sessions_world) -> None:
    w = sessions_world
    assert _svc.is_active(w.live)
    assert not _svc.is_active(w.old)
    w.live.ended_at = w.live.last_activity_at
    assert not _svc.is_active(w.live)


async def test_usage_by_session_matches_by_client_id_else_by_time(db_session: AsyncSession, sessions_world) -> None:
    from decimal import Decimal

    from specivo.services.agent_session_usage import session_entries, usage_by_session

    w = sessions_world
    usage = await usage_by_session(db_session, w.admin, [w.live.id, w.hidden.id, w.old.id])
    # Excluded entries are left out of the totals.
    assert usage[w.live.id].entries == 1
    assert [(m.amount, m.currency) for m in usage[w.live.id].spend] == [(Decimal("1.50"), "USD")]
    assert usage[w.hidden.id].entries == 1
    assert [(m.amount, m.currency) for m in usage[w.hidden.id].spend] == [(Decimal("2.00"), "USD")]
    assert w.old.id not in usage

    entries, total = await session_entries(db_session, w.admin, w.live)
    assert total == 2
    assert {e.external_session_id for e in entries} == {f"{'ass'}-live"}

    # Entries are read with the viewer's AI usage visibility: the member reads none of the agent's.
    assert await usage_by_session(db_session, w.member, [w.live.id]) == {}


# ---------------------------------------------------------------------------
# Several client sessions on one connection
# ---------------------------------------------------------------------------


async def test_select_for_client_claims_a_row_without_an_id(db_session: AsyncSession, world: World) -> None:
    s = await _conn(db_session, world, "conn-a")

    row, created = await _svc.select_for_client(db_session, s.id, world.agent, "sess_a")

    assert (row.id, created) == (s.id, False)


async def test_select_for_client_adds_a_row_and_the_newest_is_current(db_session: AsyncSession, world: World) -> None:
    s = await _conn(db_session, world, "conn-a")
    await _svc.set_external(db_session, s.id, world.agent, "sess_a", None)

    row, created = await _svc.select_for_client(db_session, s.id, world.agent, "sess_b")

    assert created and row.id != s.id
    assert (row.api_key_id, row.connection_hash, row.client_type) == (s.api_key_id, s.connection_hash, s.client_type)
    assert row.external_session_id is None  # the caller records the id
    await _svc.set_external(db_session, row.id, world.agent, "sess_b", None)
    assert (await _conn(db_session, world, "conn-a")).id == row.id
    again, created = await _svc.select_for_client(db_session, row.id, world.agent, "sess_a")
    assert (again.id, created) == (s.id, False)
    assert (await _conn(db_session, world, "conn-a")).id == s.id


async def test_select_for_client_skips_a_timed_out_or_ended_row(db_session: AsyncSession, world: World) -> None:
    s = await _conn(db_session, world, "conn-a")
    await _svc.set_external(db_session, s.id, world.agent, "sess_a", None)
    b, _ = await _svc.select_for_client(db_session, s.id, world.agent, "sess_b")
    await _svc.set_external(db_session, b.id, world.agent, "sess_b", None)
    s.last_activity_at = s.last_activity_at - timedelta(days=1)
    await db_session.flush()

    fresh, created = await _svc.select_for_client(db_session, b.id, world.agent, "sess_a")
    assert created and fresh.id not in (s.id, b.id)

    await _svc.end_session(db_session, b)
    await _svc.set_external(db_session, fresh.id, world.agent, "sess_a", None)
    other, created = await _svc.select_for_client(db_session, fresh.id, world.agent, "sess_b")
    assert created and other.id != b.id


async def test_clear_issue(db_session: AsyncSession, world: World) -> None:
    s = await _conn(db_session, world, "conn-a")
    assert await _svc.clear_issue(db_session, s.id, world.agent) is None
    await _svc.record_issue(db_session, s.id, world.agent, world.issue)

    assert await _svc.clear_issue(db_session, s.id, world.agent) == world.issue.id
    assert s.issue_id is None
