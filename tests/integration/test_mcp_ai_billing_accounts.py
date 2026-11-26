"""The MCP tool ``specivo_ai_billing_accounts``: every op, who may use it, and what it audits.

Every call goes through the server's own tool entry point, so the key is
authenticated and its project limit bound as for a real client. The rules are
:class:`AiBillingAccountService`'s; these tests check the tool keeps them.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
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
from specivo.models.ai_billing_account import AiBillingAccount
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
    project = ProjectFactory.build(key="MBAC", identifier="mbac", is_public=False)
    users = {
        "alice": UserFactory.build(login="mba_alice", status="active", display_name="Alice"),
        "bob": UserFactory.build(login="mba_bob", status="active", display_name="Bob"),
        "admin": AdminUserFactory.build(login="mba_admin", status="active"),
        "runner": ServiceAccountFactory.build(login="mba_runner", status="active"),
    }
    db_session.add_all([project, *users.values()])
    await db_session.flush()
    world = World(project=project, users=users, ids={name: u.id for name, u in users.items()})
    keys = ApiKeyService()
    for name, user in users.items():
        _, raw = await keys.create_key(db_session, user_id=user.id, name=f"mba-{name}", check_scope_access=False)
        world.keys[name] = raw
    _, raw = await keys.create_key(
        db_session,
        user_id=users["alice"].id,
        name="mba-alice-limited",
        scopes={"projects": [project.key]},
        check_scope_access=False,
    )
    world.keys["alice_limited"] = raw
    await db_session.commit()
    return world


async def _call(w: World, who: str, op: str, **kw: Any) -> str:
    mcp_raw_key_var.set(w.keys[who])
    return await server.specivo_ai_billing_accounts(op=op, **kw)


async def _refused(w: World, who: str, op: str, **kw: Any) -> str:
    with pytest.raises(ToolError) as exc:
        await _call(w, who, op, **kw)
    return str(exc.value)


async def _accounts(db: AsyncSession, owner_id: int | None) -> list[AiBillingAccount]:
    column = AiBillingAccount.owner_user_id
    clause = column.is_(None) if owner_id is None else column == owner_id
    rows = await db.execute(select(AiBillingAccount).where(clause).order_by(AiBillingAccount.id))
    return list(rows.scalars().all())


async def _audit(db: AsyncSession, event: AuditEvent) -> list[SecurityAuditLog]:
    rows = await db.execute(
        select(SecurityAuditLog).where(SecurityAuditLog.event_type == event.value).order_by(SecurityAuditLog.id)
    )
    return list(rows.scalars().all())


def _id(text: str) -> int:
    match = re.search(r"#(\d+) ", text)
    assert match, text
    return int(match.group(1))


GATEWAY: dict[str, Any] = {
    "slug": "corp-gw",
    "name": "Company gateway",
    "kind": "gateway",
    "payer": "employer",
    "route_patterns": ["corp-*"],
    "default_cost_type": "api",
}


async def test_admin_creates_lists_updates_and_deactivates_an_instance_account(
    db_session: AsyncSession, w: World
) -> None:
    text = await _call(w, "admin", "create", owner="instance", **GATEWAY)
    assert text.startswith("Created #") and "[corp-gw] gateway, instance" in text and "routes: corp-*" in text
    account_id = _id(text)
    [account] = await _accounts(db_session, None)
    assert account.id == account_id and account.default_cost_type == "api"

    listed = await _call(w, "alice", "list")
    assert "corp-gw" in listed and "Instance" not in listed.splitlines()[0]
    assert "corp-gw" in await _call(w, "alice", "list", owner="instance")

    text = await _call(w, "admin", "update", account_id=account_id, payer="employee", reimburse_share=0.25)
    assert "payer employee" in text and "reimbursed 0.25" in text
    text = await _call(w, "admin", "update", account_id=account_id, reimburse_share="")
    assert "reimbursed" not in text

    text = await _call(w, "admin", "deactivate", account_id=account_id)
    assert text.startswith("Switched off") and "OFF" in text
    assert "corp-gw" not in await _call(w, "alice", "list")
    assert "corp-gw" in await _call(w, "alice", "list", include_inactive=True)
    assert "OFF" not in await _call(w, "admin", "update", account_id=account_id, is_active=True)

    changed = await _audit(db_session, AuditEvent.AI_BILLING_ACCOUNTS_CHANGED)
    assert [row.details["op"] for row in changed] == ["create", "update", "update", "deactivate", "update"]
    assert changed[0].details["owner"] == "instance" and changed[0].details["account_ids"] == [account_id]
    assert changed[1].details["fields"] == ["payer", "reimburse_share"]
    assert len(await _audit(db_session, AuditEvent.AI_BILLING_ACCOUNT_CREATED)) == 1
    assert len(await _audit(db_session, AuditEvent.AI_BILLING_ACCOUNT_DEACTIVATED)) == 1


async def test_people_manage_their_own_accounts(db_session: AsyncSession, w: World) -> None:
    text = await _call(
        w, "alice", "create", slug="my-key", name="My key", kind="api_key", payer="employee", reimburse_share="0.5"
    )
    assert "mba_alice" in text and "reimbursed 0.5" in text
    [account] = await _accounts(db_session, w.ids["alice"])
    assert account.reimburse_share == Decimal("0.5")
    account_id = account.id
    assert "Your AI billing accounts" in await _call(w, "alice", "list", owner="mba_alice")

    text = await _call(w, "alice", "update", account_id=account_id, name="Work key")
    assert "Work key" in text
    # Own changes write only the MCP row, not the administration events.
    assert await _audit(db_session, AuditEvent.AI_BILLING_ACCOUNT_CREATED) == []
    assert len(await _audit(db_session, AuditEvent.AI_BILLING_ACCOUNTS_CHANGED)) == 2

    # Nobody else sees or changes it, except administrators.
    assert "not found" in (await _refused(w, "bob", "update", account_id=account_id, name="x")).lower()
    assert "my-key" not in await _call(w, "bob", "list")
    text = await _call(w, "admin", "list", owner="mba_alice")
    assert "mba_alice's AI billing accounts" in text and "my-key" in text
    await _call(w, "admin", "deactivate", account_id=account_id)
    [row] = await _audit(db_session, AuditEvent.AI_BILLING_ACCOUNT_DEACTIVATED)
    assert row.details["owner"] == "mba_alice"


async def test_admin_creates_an_account_for_someone(db_session: AsyncSession, w: World) -> None:
    text = await _call(w, "admin", "create", owner="mba_bob", slug="bob-key", name="Bob key", kind="api_key",
                       payer="employee")  # fmt: skip
    assert "mba_bob" in text
    assert [a.slug for a in await _accounts(db_session, w.ids["bob"])] == ["bob-key"]
    assert "No user" in await _refused(w, "admin", "create", owner="nobody_here", **GATEWAY)


async def test_refusals(db_session: AsyncSession, w: World) -> None:
    corp_id = _id(await _call(w, "admin", "create", owner="instance", **GATEWAY))
    assert "Administrators" in await _refused(w, "alice", "create", owner="instance", **{**GATEWAY, "slug": "x"})
    assert "Administrators" in await _refused(w, "alice", "update", account_id=corp_id, name="Mine")
    assert "Only administrators" in await _refused(w, "alice", "list", owner="mba_bob")
    assert "unlimited" in await _refused(w, "alice_limited", "list")
    assert "Service accounts" in await _refused(w, "runner", "create", slug="r", name="R", kind="api_key",
                                                payer="employee")  # fmt: skip
    assert "needs account_id" in await _refused(w, "alice", "update", name="x")
    assert "at least one field" in await _refused(w, "admin", "update", account_id=corp_id)
    assert "needs slug" in await _refused(w, "alice", "create", name="x", kind="api_key", payer="employee")
    assert "Invalid op" in await _refused(w, "alice", "delete", account_id=corp_id)
    assert "exists" in await _refused(w, "admin", "create", owner="instance", **GATEWAY)
    text = await _refused(w, "alice", "create", slug="k", name="K", kind="api_key", payer="employer",
                          reimburse_share="0.5")  # fmt: skip
    assert "reimbursement share" in text

    await SettingsService().set_many(db_session, {SELF_SERVICE_SETTING_KEY: "false"})
    await db_session.commit()
    assert "administrator manages" in await _refused(
        w, "alice", "create", slug="k", name="K", kind="api_key", payer="employee"
    )
    assert "self-service is off" in await _call(w, "alice", "list")


async def test_subscription_tool_shows_the_account_link(db_session: AsyncSession, w: World) -> None:
    mcp_raw_key_var.set(w.keys["alice"])
    text = await server.specivo_ai_subscriptions(
        op="add", periods=[server.SubscriptionPeriod(plan_key="claude-pro", starts_on="2026-01-01")]
    )
    [account] = await _accounts(db_session, w.ids["alice"])
    assert f"billing account #{account.id}" in text
    listed = await _call(w, "alice", "list")
    assert "fee schedule of subscription #" in listed


def test_setup_guide_lists_the_tool() -> None:
    guide = generate_setup_guide(fmt="generic")
    assert "specivo_ai_billing_accounts" in guide and "ai_billing_accounts_changed" in guide
