"""The MCP tool ``specivo_ai_subscriptions`` on company seat plans: company lists and adds, the seat ops, who may.

The rules are :class:`AiSubscriptionService`'s (``test_ai_company_plans``);
these tests check the tool keeps them, reports what it did and audits it.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from mcp.server.mcpserver.exceptions import ToolError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.mcp import server
from specivo.mcp.annotations import TOOL_ANNOTATIONS
from specivo.mcp.auth import mcp_raw_key_var
from specivo.mcp.docs import generate_setup_guide
from specivo.models.ai_subscription import AiSubscription, AiSubscriptionMember
from specivo.models.project import Project
from specivo.models.security_audit import SecurityAuditLog
from specivo.models.user import User
from specivo.services.api_key_service import ApiKeyService
from specivo.services.security_audit_service import AuditEvent
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.serial]


@dataclass
class World:
    project: Project
    users: dict[str, User]
    keys: dict[str, str] = field(default_factory=dict)
    ids: dict[str, int] = field(default_factory=dict)


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
    project = ProjectFactory.build(key="MCSP", identifier="mcsp", is_public=False)
    users = {
        "alice": UserFactory.build(login="mcs_alice", status="active", display_name="Alice"),
        "bob": UserFactory.build(login="mcs_bob", status="active", display_name="Bob"),
        "admin": AdminUserFactory.build(login="mcs_admin", status="active"),
    }
    db_session.add_all([project, *users.values()])
    await db_session.flush()
    world = World(project=project, users=users, ids={name: u.id for name, u in users.items()})
    keys = ApiKeyService()
    for name, user in users.items():
        _, raw = await keys.create_key(db_session, user_id=user.id, name=f"mcs-{name}", check_scope_access=False)
        world.keys[name] = raw
    _, raw = await keys.create_key(
        db_session,
        user_id=users["admin"].id,
        name="mcs-admin-limited",
        scopes={"projects": [project.key]},
        check_scope_access=False,
    )
    world.keys["admin_limited"] = raw
    await db_session.commit()
    return world


async def _call(w: World, who: str, op: str, **kw: Any) -> str:
    mcp_raw_key_var.set(w.keys[who])
    return await server.specivo_ai_subscriptions(op=op, **kw)


async def _refused(w: World, who: str, op: str, **kw: Any) -> str:
    with pytest.raises(ToolError) as exc:
        await _call(w, who, op, **kw)
    return str(exc.value)


async def _audit(db: AsyncSession, event: AuditEvent) -> list[SecurityAuditLog]:
    rows = await db.execute(
        select(SecurityAuditLog).where(SecurityAuditLog.event_type == event.value).order_by(SecurityAuditLog.id)
    )
    return list(rows.scalars().all())


TEAM = {
    "plan_name": "Claude Team",
    "provider": "anthropic",
    "tool_slugs": ["claude-code"],
    "amount": "250",
    "currency": "USD",
    "starts_on": "2026-11-01",
    "seat_count": 5,
}


async def _team(db: AsyncSession, w: World) -> AiSubscription:
    text = await _call(w, "admin", "add", company=True, periods=[server.SubscriptionPeriod(**TEAM)])
    assert text.startswith("Added 1 company AI plan(s):"), text
    [plan] = (await db.execute(select(AiSubscription).where(AiSubscription.user_id.is_(None)))).scalars().all()
    return plan


async def test_the_tool_documents_the_company_ops_and_keeps_its_hints() -> None:
    tools = {tool.name: tool for tool in await server.mcp.list_tools()}
    description = tools["specivo_ai_subscriptions"].description or ""
    for word in ("company=true", "seat_count", "add_member", "end_member", "remove_member", "members"):
        assert word in description
    hints = TOOL_ANNOTATIONS["specivo_ai_subscriptions"]
    # remove_member deletes, add_member adds: neither read-only nor idempotent, and destructive.
    assert (hints.read_only_hint, hints.destructive_hint, hints.idempotent_hint) == (False, True, False)
    assert "Register a company seat plan" in generate_setup_guide()


async def test_an_administrator_adds_a_plan_and_hands_out_seats(db_session: AsyncSession, w: World) -> None:
    db = db_session
    plan = await _team(db, w)
    assert (plan.seat_count, plan.amount, plan.plan_name) == (5, Decimal("250"), "Claude Team")
    # A refused call rolls the session back and expires loaded rows: keep plain ids.
    plan_id = plan.id

    listed = await _call(w, "admin", "list", company=True)
    assert f"#{plan_id} Claude Team [custom] anthropic, company plan, 5 seat(s), 250.00 USD a month" in listed

    text = await _call(w, "admin", "add_member", subscription_id=plan_id, user="mcs_bob", starts_on="2026-11-01")
    assert text.startswith(f"Gave mcs_bob a seat on #{plan_id} Claude Team.")
    assert "mcs_bob, from 2026-11-01, open" in text
    member_id = (await db.execute(select(AiSubscriptionMember.id))).scalar_one()

    refused = await _refused(w, "admin", "add_member", subscription_id=plan_id, user="mcs_bob", starts_on="2026-12-01")
    assert "already holds a seat" in refused

    text = await _call(w, "admin", "end_member", subscription_id=plan_id, user="mcs_bob", ends_on="2026-11-30")
    assert "mcs_bob, 2026-11-01 to 2026-11-30" in text
    ends = (await db.execute(select(AiSubscriptionMember.ends_on))).scalar_one()
    assert ends == date(2026, 11, 30)

    text = await _call(w, "admin", "members", subscription_id=plan_id)
    assert "Seats:" in text and f"member #{member_id} mcs_bob" in text

    text = await _call(w, "admin", "remove_member", subscription_id=plan_id, member_id=member_id)
    assert text.startswith(f"Removed member #{member_id}") and "(none)" in text

    changed = await _audit(db, AuditEvent.AI_SUBSCRIPTIONS_CHANGED)
    assert [r.details["op"] for r in changed] == ["add", "add_member", "end_member", "remove_member"]
    assert changed[1].details["member_id"] == member_id
    assert len(await _audit(db, AuditEvent.AI_SUBSCRIPTION_MEMBER_ADDED)) == 1
    assert len(await _audit(db, AuditEvent.AI_SUBSCRIPTION_MEMBER_REMOVED)) == 1

    # The plan itself: more seats, then delete it.
    text = await _call(w, "admin", "update", subscription_id=plan_id, seat_count=8)
    assert "8 seat(s)" in text
    await _call(w, "admin", "delete", subscription_id=plan_id)
    assert (await db.execute(select(AiSubscription.id).where(AiSubscription.id == plan_id))).first() is None


async def test_people_and_limited_keys_cannot_manage_company_plans(db_session: AsyncSession, w: World) -> None:
    plan = await _team(db_session, w)
    plan_id = plan.id
    assert "Only administrators" in await _refused(w, "alice", "list", company=True)
    assert "Only administrators" in await _refused(
        w, "alice", "add", company=True, periods=[server.SubscriptionPeriod(**TEAM)]
    )
    assert "Only administrators" in await _refused(w, "alice", "add_member", subscription_id=plan_id, user="mcs_alice")
    # A company plan is not one of alice's subscriptions.
    assert "not found" in (await _refused(w, "alice", "end", subscription_id=plan_id)).lower()
    assert "unlimited" in await _refused(w, "admin_limited", "list", company=True)
    # A personal plan has no seats, and a personal period no seat count.
    await _call(w, "alice", "add", periods=[server.SubscriptionPeriod(plan_key="claude-pro", starts_on="2026-11-01")])
    [own] = (
        (await db_session.execute(select(AiSubscription).where(AiSubscription.user_id == w.ids["alice"])))
        .scalars()
        .all()
    )
    assert "Only company plans" in await _refused(w, "admin", "members", subscription_id=own.id)
    refused = await _refused(
        w,
        "alice",
        "add",
        periods=[server.SubscriptionPeriod(plan_key="claude-pro", starts_on="2026-12-01", seat_count=2)],
    )
    assert "Only company plans have seats" in refused


async def test_a_company_plan_may_cost_nothing_but_needs_seats(db_session: AsyncSession, w: World) -> None:
    free = {**TEAM, "amount": "0", "plan_name": "Booked elsewhere"}
    text = await _call(w, "admin", "add", company=True, periods=[server.SubscriptionPeriod(**free)])
    assert "0.00 USD a month" in text
    refused = await _refused(
        w, "admin", "add", company=True, periods=[server.SubscriptionPeriod(**{**TEAM, "seat_count": None})]
    )
    assert "Nothing was added" in refused and "seats" in refused
