"""The MCP tools ``specivo_log_ai_usage`` and ``specivo_ai_usage``.

The world of the REST tests (``test_ai_usage_api.build_world``). Every call
goes through the server's own tool entry point, so the key is authenticated,
its project limit bound and its agent session tracked as for a real client.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import date
from typing import Any

import pytest
import pytest_asyncio
from mcp.server.mcpserver.exceptions import ToolError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.mcp import server
from specivo.mcp.auth import mcp_raw_key_var
from specivo.models.agent_session import AgentSession
from specivo.models.ai_usage_entry import AiUsageEntry
from specivo.models.security_audit import SecurityAuditLog
from specivo.services.ai_usage_service import AiUsageInput, AiUsageService
from specivo.services.security_audit_service import AuditEvent
from tests.integration.test_ai_usage_api import World, build_world

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.serial]

DAY = "2026-03-10"


@pytest.fixture(autouse=True)
def use_test_session(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Run every tool call on the test's rolled-back session."""

    @asynccontextmanager
    async def _session():
        yield db_session

    monkeypatch.setattr(server, "get_session_factory", lambda: _session)
    token = mcp_raw_key_var.set(None)
    yield
    mcp_raw_key_var.reset(token)


@pytest_asyncio.fixture
async def w(db_session: AsyncSession) -> World:
    return await build_world(db_session, "mau")


def _as(w: World, name: str) -> None:
    mcp_raw_key_var.set(w.keys[name])


async def _log(w: World, who: str, **kw: Any) -> str:
    _as(w, who)
    values: dict[str, Any] = {
        "tool": "claude-code",
        "model": "lab-model-1",
        "provider": "testlab",
        "input_tokens": 1_000_000,
        "output_tokens": 100_000,
        "usage_date": DAY,
        "cost_type": "api",
    }
    values.update(kw)
    return await server.specivo_log_ai_usage(**values)


def _entry_id(text: str) -> int:
    return int(text.split("#", 1)[1].split()[0])


async def _refused(call: Any) -> str:
    with pytest.raises(ToolError) as exc:
        await call
    return str(exc.value)


async def _entry(db: AsyncSession, entry_id: int) -> AiUsageEntry:
    entry = await db.get(AiUsageEntry, entry_id)
    assert entry is not None
    await db.refresh(entry)
    return entry


async def test_descriptions_say_agents_cannot_count_their_tokens() -> None:
    tools = {tool.name: tool for tool in await server.mcp.list_tools()}
    for name in ("specivo_log_ai_usage", "specivo_ai_usage"):
        assert "cannot see your own token counters" in (tools[name].description or "")
        assert "/cost" in (tools[name].description or "")
    assert "specivo_ai_usage_report" in tools


async def test_log_writes_an_mcp_entry(db_session: AsyncSession, w: World) -> None:
    text = await _log(w, "alice", issue_ref="MAUA-1", cache_read_tokens=500, comments="Login form")
    assert text.startswith("Logged AI usage entry #")
    assert "MAUA-1" in text and "API equivalent: 3" in text
    entry = await _entry(db_session, _entry_id(text))
    assert (entry.source, entry.user_id, entry.reporter_user_id) == ("mcp", w.users["alice"].id, w.users["alice"].id)
    assert entry.reporter_api_key_id == w.key_ids["alice"]
    assert (entry.issue_id, entry.attribution_method, entry.cache_read) == (w.public_issue.id, "explicit", 500)
    assert entry.comments == "Login form"
    events = (
        (
            await db_session.execute(
                select(SecurityAuditLog).where(SecurityAuditLog.event_type == AuditEvent.AI_USAGE_LOGGED.value)
            )
        )
        .scalars()
        .all()
    )
    assert [e.details["entry_id"] for e in events] == [entry.id]
    assert events[0].details["source"] == "mcp"


async def test_log_falls_back_to_the_agent_sessions_issue(db_session: AsyncSession, w: World) -> None:
    # A first call opens the key's agent session; give it the issue the agent works on.
    unassigned = await _log(w, "alice")
    assert "unassigned" in unassigned
    agent_session = (
        await db_session.execute(select(AgentSession).where(AgentSession.api_key_id == w.key_ids["alice"]))
    ).scalar_one()
    agent_session.issue_id = w.public_issue.id
    await db_session.flush()

    text = await _log(w, "alice")
    assert "the issue this agent session works on" in text
    entry = await _entry(db_session, _entry_id(text))
    assert (entry.project_id, entry.issue_id, entry.attribution_method) == (
        w.a.id,
        w.public_issue.id,
        "mcp_session",
    )
    # Without log_ai_usage there, the session's issue is not used: the entry stays unassigned.
    dave_session = await _log(w, "dave")
    assert "unassigned" in dave_session
    dave = (
        await db_session.execute(select(AgentSession).where(AgentSession.api_key_id == w.key_ids["dave"]))
    ).scalar_one()
    dave.issue_id = w.public_issue.id
    await db_session.flush()
    assert "unassigned" in await _log(w, "dave")


async def test_log_with_a_cost_and_on_behalf(db_session: AsyncSession, w: World) -> None:
    text = await _log(w, "runner", project_key="MAUA", on_behalf_of="mau_bob", cost_amount="1.25", cost_currency="USD")
    assert "for mau_bob" in text
    entry = await _entry(db_session, _entry_id(text))
    assert (entry.user_id, entry.reporter_user_id) == (w.users["bob"].id, w.users["runner"].id)
    assert entry.reported_cost is not None and str(entry.reported_cost.amount) in ("1.25", "1.25000000")
    assert "Unknown" not in text

    assert "not an active member" in await _refused(_log(w, "runner", project_key="MAUA", on_behalf_of="mau_outsider"))
    assert "No user with the login" in await _refused(_log(w, "runner", project_key="MAUA", on_behalf_of="nobody"))
    assert "needs cost_currency" in await _refused(_log(w, "alice", cost_amount="1"))
    assert "is not in project" in await _refused(_log(w, "alice", project_key="MAUB", issue_ref="MAUA-1"))
    assert "denied" in await _refused(_log(w, "dave", project_key="MAUA"))


async def test_on_behalf_hides_which_logins_exist(w: World) -> None:
    for who, extra in (
        ("alice", {"project_key": "MAUA"}),
        ("alice", {"issue_ref": "MAUA-1"}),
        ("alice", {}),
        ("runner", {}),  # unassigned for someone else: administrators only
    ):
        answers = {
            await _refused(_log(w, who, on_behalf_of=login, **extra))
            for login in ("mau_bob", "mau_outsider", "mau_eve", "mau_admin", "nobody-here")
        }
        assert answers == {"You cannot log AI usage for this user here"}, (who, extra)


async def test_list_get_and_limited_keys(db_session: AsyncSession, w: World) -> None:
    project_entry = _entry_id(await _log(w, "alice", project_key="MAUA"))
    unassigned = _entry_id(await _log(w, "alice", tool="codex"))
    by_limited = _entry_id(await _log(w, "alice_limited"))
    bob_entry = _entry_id(await _log(w, "bob", project_key="MAUA"))
    mgr_b = _entry_id(await _log(w, "mgr", project_key="MAUB"))

    _as(w, "alice")
    mine = await server.specivo_ai_usage(op="list")
    assert "logged in your name (3 total" in mine
    for entry_id in (project_entry, unassigned, by_limited):
        assert f"#{entry_id} " in mine
    only_unassigned = await server.specivo_ai_usage(op="list", unassigned=True)
    assert f"#{project_entry} " not in only_unassigned and "(2 total" in only_unassigned
    assert "(1 total" in await server.specivo_ai_usage(op="list", tool="codex")
    in_project = await server.specivo_ai_usage(op="list", project_key="MAUA")
    assert f"#{project_entry} " in in_project and f"#{bob_entry} " not in in_project

    _as(w, "carol")
    everyone = await server.specivo_ai_usage(op="list", project_key="MAUA")
    assert f"#{project_entry} " in everyone and f"#{bob_entry} " in everyone
    only_bob = await server.specivo_ai_usage(op="list", project_key="MAUA", user_login="mau_bob")
    assert f"#{bob_entry} " in only_bob and f"#{project_entry} " not in only_bob
    assert "(0 total" in await server.specivo_ai_usage(op="list", project_key="MAUA", user_login="nobody")

    _as(w, "alice")
    detail = await server.specivo_ai_usage(op="get", entry_id=project_entry)
    assert f"AI usage entry #{project_entry}" in detail and "source mcp" in detail
    assert "not found" in await _refused(server.specivo_ai_usage(op="get", entry_id=bob_entry))

    # A project-limited key: unassigned entries only where it reported them, nothing outside its projects.
    _as(w, "alice_limited")
    limited = await server.specivo_ai_usage(op="list")
    assert f"#{by_limited} " in limited and f"#{unassigned} " not in limited
    _as(w, "mgr_limited")
    assert "not found" in (await _refused(server.specivo_ai_usage(op="get", entry_id=mgr_b))).lower()
    assert "not found" in (await _refused(server.specivo_ai_usage(op="list", project_key="MAUB"))).lower()
    assert "not found" in (await _refused(_log(w, "mgr_limited", project_key="MAUB"))).lower()


async def test_update_exclude_and_include(db_session: AsyncSession, w: World) -> None:
    entry_id = _entry_id(await _log(w, "alice"))
    _as(w, "alice")
    text = await server.specivo_ai_usage(
        op="update", entry_id=entry_id, issue_ref="MAUA-1", input_tokens=2_000_000, comments="Redone"
    )
    assert "MAUA-1" in text and "API equivalent: 5" in text and "Comments: Redone" in text
    entry = await _entry(db_session, entry_id)
    assert (entry.project_id, entry.input, entry.output, entry.revision) == (w.a.id, 2_000_000, 100_000, 2)

    text = await server.specivo_ai_usage(op="update", entry_id=entry_id, project_key="")
    assert "Where: unassigned" in text
    assert "at least one field" in await _refused(server.specivo_ai_usage(op="update", entry_id=entry_id))
    assert "needs entry_id" in await _refused(server.specivo_ai_usage(op="get"))
    assert "Invalid op" in await _refused(server.specivo_ai_usage(op="delete", entry_id=entry_id))

    _as(w, "carol")
    bob_entry = _entry_id(await _log(w, "bob", project_key="MAUA"))
    _as(w, "carol")
    assert "permission" in (await _refused(server.specivo_ai_usage(op="exclude", entry_id=bob_entry))).lower()
    _as(w, "mgr")
    assert "Excluded from reports" in await server.specivo_ai_usage(op="exclude", entry_id=bob_entry)
    assert "Excluded from reports" not in await server.specivo_ai_usage(op="include", entry_id=bob_entry)
    events = (
        (
            await db_session.execute(
                select(SecurityAuditLog.event_type).where(
                    SecurityAuditLog.event_type.in_(
                        [
                            AuditEvent.AI_USAGE_CHANGED.value,
                            AuditEvent.AI_USAGE_EXCLUDED.value,
                            AuditEvent.AI_USAGE_INCLUDED.value,
                        ]
                    )
                )
            )
        )
        .scalars()
        .all()
    )
    assert sorted(events) == sorted(
        [AuditEvent.AI_USAGE_CHANGED.value] * 4
        + [AuditEvent.AI_USAGE_EXCLUDED.value, AuditEvent.AI_USAGE_INCLUDED.value]
    )


async def test_reporter_entries_keep_their_usage(db_session: AsyncSession, w: World) -> None:
    outcome = await AiUsageService().upsert(
        db_session,
        w.users["alice"],
        AiUsageInput(
            tool="claude-code",
            provider="testlab",
            model="lab-model-1",
            usage_date=date(2026, 3, 10),
            tokens={"input": 10, "output": 1},
            project_id=w.a.id,
            idempotency_key="mau-reporter-1",
        ),
        source="reporter",
    )
    await db_session.commit()
    entry_id = outcome.entry.id
    _as(w, "alice")
    assert "only the project, the issue and the comments" in await server.specivo_ai_usage(op="get", entry_id=entry_id)
    refused = await _refused(server.specivo_ai_usage(op="update", entry_id=entry_id, input_tokens=5))
    assert "only the project, the issue and the comments can change" in refused
    text = await server.specivo_ai_usage(op="update", entry_id=entry_id, issue_ref="MAUA-1", comments="Login")
    assert "MAUA-1" in text
    assert "Excluded from reports" in await server.specivo_ai_usage(op="exclude", entry_id=entry_id)
