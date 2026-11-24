"""The MCP tool ``specivo_ai_spend_budgets``: the ops, who may use them, and what they audit.

Every call goes through the server's own tool entry point, so the key is
authenticated and its project limit bound as for a real client. The rules are
:class:`AiSpendBudgetService`'s; these tests check the tool keeps them. The
world is ``test_ai_spend_billing_reports``'s with the prefix ``bsm``, measured
in March 2026 (``Alice key`` 14 USD, ``Corp gateway`` 20 USD).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio
from mcp.server.mcpserver.exceptions import ToolError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.mcp import server
from specivo.mcp.annotations import TOOL_ANNOTATIONS
from specivo.mcp.auth import mcp_raw_key_var
from specivo.mcp.docs import generate_setup_guide
from specivo.models.security_audit import SecurityAuditLog
from specivo.services.api_key_service import ApiKeyService
from specivo.services.security_audit_service import AuditEvent
from tests.services.test_ai_spend_billing_reports import Billing, build_billing

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.serial]


@pytest.fixture(autouse=True)
def use_test_session(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Run every tool call on the test's rolled-back session, in March 2026."""

    @asynccontextmanager
    async def _session():
        yield db_session

    monkeypatch.setattr(server, "get_session_factory", lambda: _session)
    token = mcp_raw_key_var.set(None)
    with patch("specivo.services.ai_spend_budget_service.utcnow", return_value=datetime(2026, 3, 31, tzinfo=UTC)):
        yield
    mcp_raw_key_var.reset(token)


@pytest_asyncio.fixture
async def b(db_session: AsyncSession) -> Billing:
    return await build_billing(db_session, "bsm")


@pytest_asyncio.fixture
async def keys(db_session: AsyncSession, b: Billing) -> dict[str, str]:
    out: dict[str, str] = {}
    service = ApiKeyService()
    for name in ("alice", "bob", "admin"):
        _, raw = await service.create_key(
            db_session, user_id=b.w.users[name].id, name=f"bsm-{name}", check_scope_access=False
        )
        out[name] = raw
    _, raw = await service.create_key(
        db_session,
        user_id=b.w.alice.id,
        name="bsm-alice-limited",
        scopes={"projects": [b.w.p.key]},
        check_scope_access=False,
    )
    out["alice_limited"] = raw
    await db_session.commit()
    return out


async def _call(keys: dict[str, str], who: str, op: str, **kw: Any) -> str:
    mcp_raw_key_var.set(keys[who])
    return await server.specivo_ai_spend_budgets(op=op, **kw)


def _id(text: str) -> int:
    found = re.search(r"#(\d+) ", text)
    assert found, text
    return int(found.group(1))


async def test_a_person_manages_the_budget_of_their_own_account(
    db_session: AsyncSession, b: Billing, keys: dict[str, str]
) -> None:
    created = await _call(
        keys, "alice", "create", billing_account="bsm-alice-key", amount=20, currency="usd", thresholds=[50, 100]
    )
    assert created.startswith("Set #") and "Alice key" in created and "monthly 20.00 USD" in created
    assert "2026-03 so far 14.00 USD (70.0%)" in created and "alerts at 50%, 100%" in created
    budget_id = _id(created)

    listed = await _call(keys, "alice", "list")
    assert "AI spend budgets (1)" in listed and f"#{budget_id} " in listed
    assert "(0)" in await _call(keys, "bob", "list")

    updated = await _call(keys, "alice", "update", budget_id=budget_id, amount="14", is_active=False)
    assert "(100.0%)" in updated and "OFF" in updated

    # Not bob's to see or change; not a company account or a payer for alice.
    with pytest.raises(ToolError):
        await _call(keys, "bob", "update", budget_id=budget_id, amount="1")
    with pytest.raises(ToolError):
        await _call(keys, "alice", "create", billing_account="bsm-gateway", amount="5", currency="USD")
    with pytest.raises(ToolError):
        await _call(keys, "alice", "create", payer="employer", amount="5", currency="USD")
    with pytest.raises(ToolError):
        await _call(keys, "alice", "create", billing_account="bsm-mgr-key", amount="5", currency="USD")
    with pytest.raises(ToolError):
        await _call(keys, "alice", "update", budget_id=budget_id)
    with pytest.raises(ToolError):
        await _call(keys, "alice", "rename")
    # Budgets are not a project's: a project-limited key is refused.
    with pytest.raises(ToolError):
        await _call(keys, "alice_limited", "list")

    removed = await _call(keys, "alice", "delete", budget_id=budget_id)
    assert removed.startswith("Removed #")

    rows = await db_session.execute(
        select(SecurityAuditLog).where(SecurityAuditLog.event_type == AuditEvent.AI_SPEND_BUDGETS_CHANGED.value)
    )
    ops = [r.details["op"] for r in rows.scalars().all()]
    assert ops == ["create", "update", "delete"]


async def test_administrators_budget_company_accounts_and_payers(b: Billing, keys: dict[str, str]) -> None:
    gateway = await _call(keys, "admin", "create", billing_account=str(b.gateway.id), amount="25", currency="USD")
    assert "company account" in gateway and "(80.0%)" in gateway
    employer = await _call(keys, "admin", "create", payer="employer", amount="1000", currency="USD")
    assert "Employer total" in employer
    with pytest.raises(ToolError):
        await _call(
            keys, "admin", "create", payer="employer", billing_account="bsm-gateway", amount="1", currency="USD"
        )
    with pytest.raises(ToolError):
        await _call(keys, "admin", "create", payer="employer", amount="1", currency="USD")  # it has one

    listed = await _call(keys, "admin", "list")
    assert "AI spend budgets (2)" in listed
    # The company budgets stay the administrators'.
    with pytest.raises(ToolError):
        await _call(keys, "alice", "delete", budget_id=_id(gateway))


async def test_the_tool_is_annotated_and_documented() -> None:
    hints = TOOL_ANNOTATIONS["specivo_ai_spend_budgets"]
    assert hints.read_only_hint is False and hints.destructive_hint is True and hints.idempotent_hint is False
    guide = generate_setup_guide()
    assert "specivo_ai_spend_budgets" in guide and "### Budget AI spend" in guide
