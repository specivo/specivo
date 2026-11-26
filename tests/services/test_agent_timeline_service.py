"""AgentTimelineService: an issue's agent sessions grouped by lineage, with counters, steps and cost.

The world (``tests/integration/agent_timeline_world.py``): on issue ``pub`` the
MCP session ``live`` (child of ``old``) and the webhook half ``hermes`` of the
same run, with the subagent ``sub``; on a private sub-issue the session ``child``.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.utils import utcnow
from specivo.models.agent_session import AgentSession
from specivo.services.agent_timeline_service import AgentTimelineService, node_key
from specivo.services.permission_service import clear_role_cache
from tests.integration.agent_timeline_world import TimelineWorld, build_timeline_world

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.service]

_svc = AgentTimelineService()


@pytest_asyncio.fixture
async def world(db_session: AsyncSession):
    clear_role_cache(db_session)
    yield await build_timeline_world(db_session, "ATS")
    clear_role_cache(db_session)


async def test_one_block_with_its_run_and_subagent(db_session: AsyncSession, world: TimelineWorld) -> None:
    t, w = world, world.w
    blocks = await _svc.sessions_for_issue(db_session, w.manager, w.p, w.pub)
    assert len(blocks) == 1
    block = blocks[0]
    assert block.root.ids == [w.old.id]
    children = {node.key: node for node in block.child_nodes()}
    # The MCP and webhook halves of one run are one node; the subagent nests under the root.
    assert sorted(children[node_key(w.live)].ids) == sorted([w.live.id, t.hermes.id])
    assert children[node_key(t.sub)].ids == [t.sub.id]
    assert w.hidden.id not in block.session_ids
    assert t.child.id not in block.session_ids


async def test_counters_and_states(db_session: AsyncSession, world: TimelineWorld) -> None:
    t, w = world, world.w
    [block] = await _svc.sessions_for_issue(db_session, w.manager, w.p, w.pub)
    counters = (await _svc.counters_for(db_session, w.p, [block]))[block.root.key]
    # whoami (project-free), create_issue (in P) and the Hermes terminal call; not Q's rows.
    assert counters.tool_calls == 3
    assert counters.approvals_requested == 1
    assert counters.approvals_resolved == {"deny": 1}
    children = {node.key: node for node in block.child_nodes()}
    assert _svc.state_of(block.root, counters) == "completed"
    assert _svc.state_of(children[node_key(w.live)], counters) == "running"
    assert _svc.state_of(children[node_key(t.sub)], counters) == "interrupted"


async def test_steps_merge_the_block_and_leave_other_projects_out(
    db_session: AsyncSession, world: TimelineWorld
) -> None:
    w = world.w
    [block] = await _svc.sessions_for_issue(db_session, w.manager, w.p, w.pub)
    rows, total = await _svc.steps_for(db_session, w.p, block)
    assert total == 7
    tools = {r.details.get("tool") for r in rows}
    assert {"whoami", "terminal", "create_issue_ats"} <= tools
    assert all(r.project_id in (None, w.p.id) for r in rows)
    assert "show_issue" not in tools
    assert [r.created_at for r in rows] == sorted((r.created_at for r in rows), reverse=True)


async def test_cost_of_the_block(db_session: AsyncSession, world: TimelineWorld) -> None:
    w = world.w
    [block] = await _svc.sessions_for_issue(db_session, w.manager, w.p, w.pub)
    cost = (await _svc.cost_for(db_session, w.manager, w.p, [block]))[block.root.key]
    # 1.50 on the run and 0.25 on the subagent; the excluded 9.00 is left out.
    assert cost.entries == 2
    assert cost.marginal is not None
    assert cost.marginal.total.amount == Decimal("1.75")
    assert cost.profiles == ["night"]
    assert cost.unattended is True
    assert cost.tokens == 2 * 1100


async def test_own_visibility_sees_no_service_account_cost(db_session: AsyncSession, world: TimelineWorld) -> None:
    t, w = world, world.w
    [block] = await _svc.sessions_for_issue(db_session, t.dev, w.p, w.pub)
    assert await _svc.cost_for(db_session, t.dev, w.p, [block]) == {}


async def test_sub_issues_and_a_hidden_one(db_session: AsyncSession, world: TimelineWorld) -> None:
    t, w = world, world.w
    manager_blocks = await _svc.sessions_for_issue(db_session, w.manager, w.p, w.pub, include_subtree=True)
    assert [b.root.ids for b in manager_blocks] == [[w.old.id], [t.child.id]]
    # The lead may not see the private sub-issue, so its session stays out.
    dev_blocks = await _svc.sessions_for_issue(db_session, t.dev, w.p, w.pub, include_subtree=True)
    assert [b.root.ids for b in dev_blocks] == [[w.old.id]]


async def test_page_of_many_sessions_is_bounded(db_session: AsyncSession, world: TimelineWorld) -> None:
    w = world.w
    now = utcnow()
    db_session.add_all(
        [
            AgentSession(
                api_key_id=w.live.api_key_id,
                user_id=w.agent.id,
                issue_id=w.pub.id,
                client_type="mcp",
                client_name="bulk-agent",
                connection_hash=f"{i:064d}",
                started_at=now - timedelta(days=3, minutes=i + 1),
                last_activity_at=now - timedelta(days=3, minutes=i),
            )
            for i in range(200)
        ]
    )
    await db_session.commit()

    statements: list[str] = []

    def count(_conn, _cursor, statement, *_args) -> None:
        statements.append(statement)

    engine = (await db_session.connection()).sync_connection.engine
    event.listen(engine, "before_cursor_execute", count)
    try:
        page = await _svc.page(db_session, w.manager, w.p, w.pub, offset=20, limit=20)
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert page.total == 201
    assert len(page.blocks) == 20
    assert page.offset == 20
    assert len(statements) <= 30
