"""An API key limited to some projects reaches those projects and nothing else, over MCP.

The same world as the REST tests (``test_api_key_project_scope``): the owner is
a member of A and B, C is public, and the key is limited to A. Each tool is
called through the server's own entry point, so the key is authenticated and
its limit bound exactly as for a real client.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from mcp.server.mcpserver.exceptions import ToolError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.mcp import server
from specivo.mcp.auth import mcp_raw_key_var
from specivo.models.time_entry import ActiveTimer, TimeEntry
from specivo.services.api_key_scope import current_project_scope
from tests.integration.test_api_key_project_scope import World, build_world

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.serial]


@pytest.fixture
def use_test_session(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Run every tool call on the test's rolled-back session."""

    @asynccontextmanager
    async def _session():
        yield db_session

    monkeypatch.setattr(server, "get_session_factory", lambda: _session)
    token = mcp_raw_key_var.set(None)
    yield
    mcp_raw_key_var.reset(token)


def _as(raw_key: str) -> None:
    mcp_raw_key_var.set(raw_key)


async def _refused(call) -> str:
    with pytest.raises(ToolError) as exc:
        await call
    return str(exc.value)


async def _world(db_session: AsyncSession, client: AsyncClient, prefix: str, **kwargs) -> World:
    world = await build_world(db_session, client, prefix, **kwargs)
    client.headers.pop("Authorization", None)
    return world


async def test_reads_outside_the_limit_are_not_found(
    use_test_session, client: AsyncClient, db_session: AsyncSession
) -> None:
    w = await _world(db_session, client, "KMR")
    # Plain strings: a refused call rolls back and expires the ORM rows.
    a_key, b_key, c_key = w.a.key, w.b.key, w.c.key
    _as(w.limited_raw)

    for ref in (w.b_issue["key"], w.c_issue["key"]):
        assert "not found" in await _refused(server.specivo_show_issue(issue_ref=ref))
        assert "not found" in await _refused(server.specivo_list_comments(issue_ref=ref))
        assert "not found" in await _refused(server.specivo_list_relations(issue_ref=ref))
    for project_key in (b_key, c_key):
        assert "not found" in await _refused(server.specivo_list_issues(project_key=project_key))
        assert "not found" in await _refused(server.specivo_read_wiki(project_key=project_key, slug="handbook"))
        assert "not found" in await _refused(server.specivo_list_members(project_key=project_key))
    assert "not found" in await _refused(server.specivo_search(query="quokka", project_key=b_key))

    projects = await server.specivo_list_projects()
    assert a_key in projects
    assert b_key not in projects and c_key not in projects

    found = await server.specivo_search(query="quokka", mode="keyword")
    assert w.a_issue["key"] in found or "alpha quokka" in found
    assert "beta quokka" not in found and "gamma quokka" not in found

    relations = await server.specivo_list_relations(issue_ref=w.a_issue["key"])
    assert w.b_issue["key"] not in relations

    history = await server.specivo_list_comments(issue_ref=w.a_issue["key"], include_changes=True)
    assert w.b_version_name not in history
    assert w.b_issue["key"] not in history

    assert "alpha quokka" in await server.specivo_show_issue(issue_ref=w.a_issue["key"])
    assert "handbook" in (await server.specivo_read_wiki(project_key=a_key, slug="handbook")).lower()
    assert current_project_scope() is None


async def test_writes_outside_the_limit_are_refused_and_inside_succeed(
    use_test_session, client: AsyncClient, db_session: AsyncSession
) -> None:
    w = await _world(db_session, client, "KMW")
    # Plain strings: a refused call rolls back and expires the ORM rows.
    a_key, b_key, c_key = w.a.key, w.b.key, w.c.key
    _as(w.limited_raw)

    assert "not found" in await _refused(
        server.specivo_create_issue(project_key=b_key, tracker_id=w.tracker_id, subject="nope")
    )
    assert "not found" in await _refused(server.specivo_add_comment(issue_ref=w.b_issue["key"], notes="nope"))
    assert "not found" in await _refused(server.specivo_update_issue(issue_ref=w.b_issue["key"], subject="nope"))
    assert "not found" in await _refused(
        server.specivo_log_time(project_key=b_key, hours=1.0, activity_id=w.activity_id)
    )
    assert "not found" in await _refused(server.specivo_create_wiki(project_key=b_key, title="Injected", text="nope"))
    assert "not found" in await _refused(
        server.specivo_add_relation(issue_ref=w.a_issue["key"], issue_to_key=w.b_issue["key"], relation_type="relates")
    )
    assert "not found" in await _refused(
        server.specivo_move_issue(issue_ref=w.a_issue["key"], target_project_key=b_key)
    )

    created = await server.specivo_create_issue(project_key=a_key, tracker_id=w.tracker_id, subject="KMW new")
    assert f"{a_key}-" in created
    assert "Added comment" in await server.specivo_add_comment(issue_ref=w.a_issue["key"], notes="fine")
    logged = await server.specivo_log_time(project_key=a_key, hours=1.0, activity_id=w.activity_id)
    assert logged.startswith("Logged ")
    assert current_project_scope() is None


async def test_an_unlimited_key_is_unaffected(use_test_session, client: AsyncClient, db_session: AsyncSession) -> None:
    w = await _world(db_session, client, "KMU")
    # Plain strings: a refused call rolls back and expires the ORM rows.
    a_key, b_key, c_key = w.a.key, w.b.key, w.c.key
    _as(w.open_raw)

    assert "beta quokka" in await server.specivo_show_issue(issue_ref=w.b_issue["key"])
    projects = await server.specivo_list_projects()
    assert a_key in projects and b_key in projects
    relations = await server.specivo_list_relations(issue_ref=w.a_issue["key"])
    assert w.b_issue["key"] in relations
    history = await server.specivo_list_comments(issue_ref=w.a_issue["key"], include_changes=True)
    assert w.b_version_name in history


async def test_an_admin_key_limited_to_a_project_stops_at_it(
    use_test_session, client: AsyncClient, db_session: AsyncSession
) -> None:
    w = await _world(db_session, client, "KMA", admin=True)
    # Plain strings: a refused call rolls back and expires the ORM rows.
    a_key, b_key, c_key = w.a.key, w.b.key, w.c.key
    _as(w.limited_raw)

    assert "not found" in await _refused(server.specivo_show_issue(issue_ref=w.b_issue["key"]))
    assert "not found" in await _refused(server.specivo_show_issue(issue_ref=w.c_issue["key"]))
    projects = await server.specivo_list_projects()
    assert b_key not in projects and c_key not in projects
    assert "alpha quokka" in await server.specivo_show_issue(issue_ref=w.a_issue["key"])


async def test_search_refuses_every_unreachable_project_like_a_missing_one(
    use_test_session, client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing, private-without-membership, outside-the-limit and forbidden all give one tool error."""
    from specivo.core.exceptions import PermissionDeniedError
    from specivo.mcp import tools
    from specivo.services.api_key_service import ApiKeyService

    w = await _world(db_session, client, "KMU")
    _key, b_only_raw = await ApiKeyService().create_key(session=db_session, user_id=w.b_only_user.id, name="kmu-bonly")
    await db_session.commit()
    # Plain strings: a refused call rolls back and expires the ORM rows.
    a_key, b_key = w.a.key, w.b.key

    async def refusal(raw: str, project_key: str) -> str:
        _as(raw)
        message = await _refused(server.specivo_search(query="quokka", project_key=project_key, mode="keyword"))
        return message.replace(project_key, "<KEY>")

    missing = await refusal(b_only_raw, "KMUNONE")
    assert "not found" in missing
    assert await refusal(b_only_raw, a_key) == missing  # private, not a member
    assert await refusal(w.limited_raw, b_key) == missing  # outside the key's limit

    async def forbid(_session, _project, _user):
        raise PermissionDeniedError("You may not search this project")

    monkeypatch.setattr(tools._project_svc, "require_project_access", forbid)
    assert await refusal(w.limited_raw, a_key) == missing


async def test_the_timer_outside_the_limit_is_out_of_reach(
    use_test_session, client: AsyncClient, db_session: AsyncSession
) -> None:
    w = await _world(db_session, client, "KMT")
    owner_id, b_id = w.owner.id, w.b.id
    db_session.add(
        ActiveTimer(user_id=owner_id, project_id=b_id, issue_id=w.b_issue["id"], started_at=datetime.now(UTC))
    )
    await db_session.commit()
    _as(w.limited_raw)

    assert "No timer is running" in await server.specivo_timer(op="get")
    assert "No active timer" in await _refused(server.specivo_timer(op="stop"))
    assert "No active timer" in await _refused(server.specivo_timer(op="discard"))
    assert "not found" in (await _refused(server.specivo_timer(op="start", issue_ref=w.b_issue["key"]))).lower()
    assert "cannot reach" in await _refused(server.specivo_timer(op="start", issue_ref=w.a_issue["key"]))

    timer = (await db_session.execute(select(ActiveTimer).where(ActiveTimer.user_id == owner_id))).scalar_one()
    assert timer.project_id == b_id
    entries = (await db_session.execute(select(TimeEntry).where(TimeEntry.user_id == owner_id))).scalars().all()
    assert [e.id for e in entries] == [w.b_entry_id]
    assert current_project_scope() is None
