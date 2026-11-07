"""The MCP tool ``specivo_ai_subscriptions``: every op, who may use it, all-or-nothing adds and warnings.

Every call goes through the server's own tool entry point, so the key is
authenticated and its project limit bound as for a real client. The rules
themselves are :class:`AiSubscriptionService`'s (``test_ai_subscriptions``);
these tests check the tool keeps them and says what it did.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from mcp.server.mcpserver.exceptions import ToolError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.mcp import server
from specivo.mcp.auth import mcp_raw_key_var
from specivo.mcp.docs import generate_setup_guide
from specivo.models.ai_subscription import AiSubscription
from specivo.models.project import Project
from specivo.models.security_audit import SecurityAuditLog
from specivo.models.user import User
from specivo.services.ai_subscription_service import SELF_SERVICE_SETTING_KEY
from specivo.services.api_key_service import ApiKeyService
from specivo.services.security_audit_service import AuditEvent
from specivo.services.settings_service import SettingsService
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, ServiceAccountFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.serial]


@dataclass
class World:
    project: Project
    users: dict[str, User]
    keys: dict[str, str] = field(default_factory=dict)
    #: User ids, readable after a refused call has rolled the session back (and expired the users).
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
    project = ProjectFactory.build(key="MASA", identifier="masa", is_public=False)
    users = {
        "alice": UserFactory.build(login="mas_alice", status="active", display_name="Alice"),
        "bob": UserFactory.build(login="mas_bob", status="active", display_name="Bob"),
        "admin": AdminUserFactory.build(login="mas_admin", status="active"),
        "runner": ServiceAccountFactory.build(login="mas_runner", status="active"),
    }
    db_session.add_all([project, *users.values()])
    await db_session.flush()
    world = World(project=project, users=users, ids={name: u.id for name, u in users.items()})
    keys = ApiKeyService()
    for name, user in users.items():
        _, raw = await keys.create_key(db_session, user_id=user.id, name=f"mas-{name}", check_scope_access=False)
        world.keys[name] = raw
    for name in ("alice", "admin"):
        _, raw = await keys.create_key(
            db_session,
            user_id=users[name].id,
            name=f"mas-{name}-limited",
            scopes={"projects": [project.key]},
            check_scope_access=False,
        )
        world.keys[f"{name}_limited"] = raw
    await db_session.commit()
    return world


async def _call(w: World, who: str, op: str, **kw: Any) -> str:
    mcp_raw_key_var.set(w.keys[who])
    return await server.specivo_ai_subscriptions(op=op, **kw)


async def _refused(w: World, who: str, op: str, **kw: Any) -> str:
    with pytest.raises(ToolError) as exc:
        await _call(w, who, op, **kw)
    return str(exc.value)


def _periods(*rows: dict[str, Any]) -> list[server.SubscriptionPeriod]:
    return [server.SubscriptionPeriod(**row) for row in rows]


async def _subs(db: AsyncSession, user_id: int) -> list[AiSubscription]:
    rows = await db.execute(
        select(AiSubscription).where(AiSubscription.user_id == user_id).order_by(AiSubscription.starts_on)
    )
    return list(rows.scalars().all())


async def _audit(db: AsyncSession, event: AuditEvent) -> list[SecurityAuditLog]:
    rows = await db.execute(
        select(SecurityAuditLog).where(SecurityAuditLog.event_type == event.value).order_by(SecurityAuditLog.id)
    )
    return list(rows.scalars().all())


def _ids(text: str) -> list[int]:
    return [int(m) for m in re.findall(r"#(\d+) ", text)]


#: The motivating case: one person's Claude and ChatGPT invoices.
INVOICES = (
    {"plan_key": "claude-max-5x", "amount": "100", "currency": "USD", "starts_on": "2026-01-21",
     "ends_on": "2026-02-25"},
    {"plan_key": "claude-max-20x", "amount": "200", "currency": "USD", "starts_on": "2026-03-26",
     "ends_on": "2026-05-25"},
    {"plan_key": "claude-max-5x", "amount": "100", "currency": "USD", "starts_on": "2026-05-26",
     "ends_on": "2026-07-25"},
    {"plan_key": "claude-max-5x", "amount": "112", "currency": "USD", "starts_on": "2026-07-26",
     "notes": "Price with tax"},
    {"plan_key": "chatgpt-plus", "amount": "20", "currency": "USD", "starts_on": "2026-04-15",
     "notes": "VAT included"},
)  # fmt: skip


# ---------------------------------------------------------------------------
# Registration and docs
# ---------------------------------------------------------------------------


async def test_the_tool_is_registered_and_documented() -> None:
    tools = {tool.name: tool for tool in await server.mcp.list_tools()}
    description = tools["specivo_ai_subscriptions"].description or ""
    for word in ("list", "presets", "add", "update", "end", "delete", "all or nothing", "project-limited"):
        assert word in description
    guide = generate_setup_guide()
    assert "`specivo_ai_subscriptions`" in guide
    assert "Register AI subscriptions from invoices" in guide


# ---------------------------------------------------------------------------
# The invoice case
# ---------------------------------------------------------------------------


async def test_five_invoices_in_one_add_with_a_gap_warning(db_session: AsyncSession, w: World) -> None:
    text = await _call(w, "alice", "add", periods=_periods(*INVOICES))
    assert text.startswith("Added 5 subscription period(s) to your AI subscriptions:")
    subs = await _subs(db_session, w.ids["alice"])
    assert [(s.plan_key, s.amount, s.starts_on, s.ends_on) for s in subs] == [
        ("claude-max-5x", Decimal("100"), date(2026, 1, 21), date(2026, 2, 25)),
        ("claude-max-20x", Decimal("200"), date(2026, 3, 26), date(2026, 5, 25)),
        ("chatgpt-plus", Decimal("20"), date(2026, 4, 15), None),
        ("claude-max-5x", Decimal("100"), date(2026, 5, 26), date(2026, 7, 25)),
        ("claude-max-5x", Decimal("112"), date(2026, 7, 26), None),
    ]
    assert sorted(_ids(text.split("Warnings")[0])) == sorted(s.id for s in subs)
    # Each line shows the plan, amount, currency, dates and the tools covered.
    assert "Claude Max 5x [claude-max-5x] anthropic, 112.00 USD a month, from 2026-07-26, open" in text
    assert "covers: Claude Code (the tools of anthropic)" in text
    assert "ChatGPT Plus [chatgpt-plus] openai, 20.00 USD a month, from 2026-04-15, open" in text
    assert "Codex (the tools of openai)" in text
    assert "notes: VAT included" in text
    # The month without a Claude plan is a warning, the only one; nothing is refused.
    warnings = text.split("Warnings", 1)[1].splitlines()[1:]
    assert len(warnings) == 1
    assert "Gap in anthropic plans: none from 2026-02-26 to 2026-03-25" in warnings[0]
    assert "Overlap" not in text

    listed = await _call(w, "alice", "list")
    assert "AI subscriptions, your (5 total, showing 1-5)" in listed
    assert "Gap in anthropic plans: none from 2026-02-26 to 2026-03-25" in listed


# ---------------------------------------------------------------------------
# list and presets
# ---------------------------------------------------------------------------


async def test_presets_list_every_plan_with_its_price(w: World) -> None:
    text = await _call(w, "alice", "presets")
    assert "claude-max-20x: Claude Max 20x, anthropic, 200.00 USD a month" in text
    assert "cursor-pro: Cursor Pro, cursor, 20.00 USD a month; covers: Cursor" in text
    assert "custom: any other plan" in text
    # A lookup: open to a limited key and a service account too.
    assert "claude-pro" in await _call(w, "alice_limited", "presets")
    assert "claude-pro" in await _call(w, "runner", "presets")


async def test_list_shows_only_your_own(w: World) -> None:
    await _call(w, "bob", "add", periods=_periods({"plan_key": "claude-pro", "starts_on": "2026-01-01"}))
    text = await _call(w, "alice", "list")
    assert "(0 total, showing none)" in text and "(none)" in text
    assert "Claude Pro" in await _call(w, "bob", "list")
    running = await _call(w, "bob", "list", active_on="2025-12-31")
    assert "(0 total" in running


async def test_admin_lists_and_adds_for_someone_else(db_session: AsyncSession, w: World) -> None:
    text = await _call(
        w, "admin", "add", user="mas_bob", periods=_periods({"plan_key": "claude-pro", "starts_on": "2026-02-01"})
    )
    assert "to mas_bob's AI subscriptions" in text
    [sub] = await _subs(db_session, w.ids["bob"])
    assert sub.created_by_id == w.ids["admin"]
    assert "AI subscriptions, mas_bob's" in await _call(w, "admin", "list", user="MAS_BOB")
    assert "No user with the login" in await _refused(w, "admin", "list", user="nobody_here")
    assert "service or system account" in await _refused(w, "admin", "list", user="mas_runner")


async def test_people_cannot_reach_someone_elses(db_session: AsyncSession, w: World) -> None:
    await _call(w, "bob", "add", periods=_periods({"plan_key": "claude-pro", "starts_on": "2026-01-01"}))
    [sub] = await _subs(db_session, w.ids["bob"])
    sub_id = sub.id
    for op, kw in (
        ("list", {"user": "mas_bob"}),
        ("add", {"user": "mas_bob", "periods": _periods({"plan_key": "claude-pro", "starts_on": "2026-01-01"})}),
    ):
        assert "Only administrators" in await _refused(w, "alice", op, **kw)
    for op, kw in (("update", {"amount": "1"}), ("end", {}), ("delete", {})):
        assert "not found" in await _refused(w, "alice", op, subscription_id=sub_id, **kw)
    assert len(await _subs(db_session, w.ids["bob"])) == 1


# ---------------------------------------------------------------------------
# Keys and accounts
# ---------------------------------------------------------------------------


async def test_a_project_limited_key_is_refused(db_session: AsyncSession, w: World) -> None:
    await _call(w, "alice", "add", periods=_periods({"plan_key": "claude-pro", "starts_on": "2026-01-01"}))
    [sub] = await _subs(db_session, w.ids["alice"])
    calls: list[tuple[str, str, dict[str, Any]]] = [
        ("alice_limited", "list", {}),
        ("alice_limited", "add", {"periods": _periods({"plan_key": "claude-pro", "starts_on": "2026-03-01"})}),
        ("alice_limited", "update", {"subscription_id": sub.id, "amount": "25"}),
        ("alice_limited", "end", {"subscription_id": sub.id}),
        ("alice_limited", "delete", {"subscription_id": sub.id}),
        ("admin_limited", "list", {"user": "mas_alice"}),
    ]
    for who, op, kw in calls:
        message = await _refused(w, who, op, **kw)
        assert "unlimited personal API key" in message, (who, op)
    [still] = await _subs(db_session, w.ids["alice"])
    assert (still.amount, still.ends_on) == (Decimal("20"), None)


async def test_a_service_account_is_refused(db_session: AsyncSession, w: World) -> None:
    for op, kw in (
        ("list", {}),
        ("add", {"periods": _periods({"plan_key": "claude-pro", "starts_on": "2026-01-01"})}),
        ("delete", {"subscription_id": 1}),
    ):
        message = await _refused(w, "runner", op, **kw)
        assert "belong to people" in message and "ask an administrator" in message
    assert await _subs(db_session, w.ids["runner"]) == []


async def test_self_service_off_leaves_changes_to_administrators(db_session: AsyncSession, w: World) -> None:
    await _call(w, "alice", "add", periods=_periods({"plan_key": "claude-pro", "starts_on": "2026-01-01"}))
    await SettingsService().set_many(db_session, {SELF_SERVICE_SETTING_KEY: "false"})
    await db_session.commit()
    message = await _refused(w, "alice", "add", periods=_periods({"plan_key": "claude-pro", "starts_on": "2026-03-01"}))
    assert "administrator manages AI subscriptions" in message
    assert "Self-service is off" in await _call(w, "alice", "list")
    added = await _call(
        w, "admin", "add", user="mas_alice", periods=_periods({"plan_key": "chatgpt-plus", "starts_on": "2026-03-01"})
    )
    assert "Added 1" in added


# ---------------------------------------------------------------------------
# add: all or nothing
# ---------------------------------------------------------------------------


async def test_add_is_all_or_nothing_and_lists_every_problem(db_session: AsyncSession, w: World) -> None:
    message = await _refused(
        w,
        "alice",
        "add",
        periods=_periods(
            {"plan_key": "claude-pro", "starts_on": "2026-01-01", "ends_on": "2026-01-31"},
            {"plan_key": "claude-pro", "amount": "abc", "starts_on": "2026-02-01"},
            {"plan_key": "no-such-plan", "starts_on": "2026-03-01"},
            {"plan_key": "claude-pro", "starts_on": "2026-04-01", "ends_on": "2026-03-01"},
            {"plan_key": "claude-pro", "starts_on": "April"},
        ),
    )
    assert "Nothing was added" in message
    assert "periods[0]" not in message
    for index in (1, 2, 3, 4):
        assert f"periods[{index}]" in message
    assert "decimal number" in message and "Choose a plan" in message and "before the first day" in message
    assert await _subs(db_session, w.ids["alice"]) == []
    assert await _audit(db_session, AuditEvent.AI_SUBSCRIPTIONS_CHANGED) == []


async def test_add_custom_plan_and_number_amount(db_session: AsyncSession, w: World) -> None:
    text = await _call(
        w,
        "alice",
        "add",
        periods=_periods(
            {
                "plan_key": "custom",
                "provider": "Mistral",
                "plan_name": "Le Chat Pro",
                "amount": 14.99,
                "currency": "EUR",
                "billing_period": "year",
                "starts_on": "2026-02-01",
            }
        ),
    )
    assert "Le Chat Pro [custom] mistral, 14.99 EUR a year, from 2026-02-01, open" in text
    [sub] = await _subs(db_session, w.ids["alice"])
    assert sub.amount == Decimal("14.99")
    message = await _refused(w, "alice", "add", periods=_periods({"plan_key": "custom", "starts_on": "2026-01-01"}))
    assert "provider" in message
    assert "needs periods" in await _refused(w, "alice", "add")


# ---------------------------------------------------------------------------
# update, end, delete and warnings
# ---------------------------------------------------------------------------


async def test_overlap_warning(w: World) -> None:
    text = await _call(
        w,
        "alice",
        "add",
        periods=_periods(
            {"plan_key": "claude-pro", "starts_on": "2026-01-01", "ends_on": "2026-03-31"},
            {"plan_key": "claude-max-5x", "starts_on": "2026-03-15"},
            {"plan_key": "cursor-pro", "starts_on": "2026-03-20"},
        ),
    )
    assert "Overlap in anthropic plans:" in text
    assert "both run 2026-03-15 to 2026-03-31" in text
    # Cursor Pro lists its tool: another family, nothing to warn about.
    assert "cursor plans" not in text and "Gap" not in text


async def test_update_end_and_delete(db_session: AsyncSession, w: World) -> None:
    text = await _call(
        w,
        "alice",
        "add",
        periods=_periods(
            {"plan_key": "claude-pro", "starts_on": "2026-01-01", "ends_on": "2026-01-31"},
            {"plan_key": "claude-pro", "starts_on": "2026-02-01"},
        ),
    )
    first, second = sorted(_ids(text))

    updated = await _call(w, "alice", "update", subscription_id=second, amount="22.50", notes="Invoice 7")
    assert updated.startswith(f"Updated #{second} Claude Pro [claude-pro] anthropic, 22.50 USD a month")
    assert "notes: Invoice 7" in updated
    assert "update needs" not in updated
    assert "at least one field" in await _refused(w, "alice", "update", subscription_id=second)

    ended = await _call(w, "alice", "end", subscription_id=second, ends_on="2026-04-30")
    assert ended.startswith(f"Ended #{second}") and "2026-02-01 to 2026-04-30" in ended
    today = datetime.now(UTC).date()
    ended_today = await _call(w, "alice", "end", subscription_id=second)
    assert f"2026-02-01 to {today.isoformat()}" in ended_today
    reopened = await _call(w, "alice", "update", subscription_id=second, ends_on="")
    assert "from 2026-02-01, open" in reopened
    assert "cannot be before" in await _refused(w, "alice", "end", subscription_id=second, ends_on="2025-12-31")

    # Deleting the first period of a family leaves no gap; deleting a middle one would.
    deleted = await _call(w, "alice", "delete", subscription_id=first)
    assert deleted.startswith(f"Deleted #{first} Claude Pro")
    assert [s.id for s in await _subs(db_session, w.ids["alice"])] == [second]
    assert "needs subscription_id" in await _refused(w, "alice", "delete")


async def test_moving_a_period_reports_the_gap_it_leaves(w: World) -> None:
    text = await _call(
        w,
        "alice",
        "add",
        periods=_periods(
            {"plan_key": "claude-pro", "starts_on": "2026-01-01", "ends_on": "2026-01-31"},
            {"plan_key": "claude-pro", "starts_on": "2026-02-01"},
        ),
    )
    assert "Warnings" not in text
    second = max(_ids(text))
    moved = await _call(w, "alice", "update", subscription_id=second, starts_on="2026-03-01")
    assert "Gap in anthropic plans: none from 2026-02-01 to 2026-02-28" in moved


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


async def test_audit(db_session: AsyncSession, w: World) -> None:
    # Your own change over MCP: the agent call is audited, not as an administrator's change.
    own = await _call(w, "alice", "add", periods=_periods({"plan_key": "claude-pro", "starts_on": "2026-01-01"}))
    [own_id] = _ids(own)
    assert await _audit(db_session, AuditEvent.AI_SUBSCRIPTION_CREATED) == []
    [call] = await _audit(db_session, AuditEvent.AI_SUBSCRIPTIONS_CHANGED)
    assert call.user_id == w.ids["alice"]
    assert call.details["source"] == "mcp" and call.details["tool"] == "ai_subscriptions"
    assert (call.details["op"], call.details["subscription_ids"], call.details["target_login"]) == (
        "add",
        [own_id],
        "mas_alice",
    )

    # An administrator's changes to somebody else's: the service's audit events, plus the agent calls.
    await _call(
        w, "admin", "add", user="mas_alice", periods=_periods({"plan_key": "chatgpt-plus", "starts_on": "2026-02-01"})
    )
    await _call(w, "admin", "update", subscription_id=own_id, amount="21")
    await _call(w, "admin", "delete", subscription_id=own_id)
    [created] = await _audit(db_session, AuditEvent.AI_SUBSCRIPTION_CREATED)
    [changed] = await _audit(db_session, AuditEvent.AI_SUBSCRIPTION_UPDATED)
    [deleted] = await _audit(db_session, AuditEvent.AI_SUBSCRIPTION_DELETED)
    for row in (created, changed, deleted):
        assert row.user_id == w.ids["admin"] and row.details["target_login"] == "mas_alice"
    assert changed.details["changes"]["amount"] == {"old": "20.00", "new": "21.00"}
    calls = await _audit(db_session, AuditEvent.AI_SUBSCRIPTIONS_CHANGED)
    assert [(c.details["op"], c.details.get("fields")) for c in calls] == [
        ("add", None),
        ("add", None),
        ("update", ["amount"]),
        ("delete", None),
    ]
    reads = await _audit(db_session, AuditEvent.RESOURCE_ACCESS)
    await _call(w, "alice", "list")
    after = await _audit(db_session, AuditEvent.RESOURCE_ACCESS)
    assert after[-1].details["tool"] == "ai_subscriptions" and len(after) == len(reads) + 1
