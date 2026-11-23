"""The MCP tool ``specivo_ai_reimbursement_claims``: the ops, who may use them, and what they audit.

Every call goes through the server's own tool entry point, so the key is
authenticated and its project limit bound as for a real client. The rules are
:class:`AiReimbursementClaimService`'s; these tests check the tool keeps them.
The world is ``test_ai_spend_billing_reports``'s with the prefix ``rcm``:
alice may claim 47.5 USD for March 2026.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import asynccontextmanager
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
    """Run every tool call on the test's rolled-back session, with mail queued nowhere."""

    @asynccontextmanager
    async def _session():
        yield db_session

    monkeypatch.setattr(server, "get_session_factory", lambda: _session)
    token = mcp_raw_key_var.set(None)
    with patch("specivo.tasks.notifications.send_notification_email.delay"):
        yield
    mcp_raw_key_var.reset(token)


@pytest_asyncio.fixture
async def b(db_session: AsyncSession) -> Billing:
    return await build_billing(db_session, "rcm")


@pytest_asyncio.fixture
async def keys(db_session: AsyncSession, b: Billing) -> dict[str, str]:
    out: dict[str, str] = {}
    service = ApiKeyService()
    for name in ("alice", "bob", "admin"):
        _, raw = await service.create_key(
            db_session, user_id=b.w.users[name].id, name=f"rcm-{name}", check_scope_access=False
        )
        out[name] = raw
    _, raw = await service.create_key(
        db_session,
        user_id=b.w.alice.id,
        name="rcm-alice-limited",
        scopes={"projects": [b.w.p.key]},
        check_scope_access=False,
    )
    out["alice_limited"] = raw
    await db_session.commit()
    return out


async def _call(keys: dict[str, str], who: str, op: str, **kw: Any) -> str:
    mcp_raw_key_var.set(keys[who])
    return await server.specivo_ai_reimbursement_claims(op=op, **kw)


async def _refused(keys: dict[str, str], who: str, op: str, **kw: Any) -> str:
    with pytest.raises(ToolError) as exc:
        await _call(keys, who, op, **kw)
    return str(exc.value)


async def _audit(db: AsyncSession, event: AuditEvent) -> list[SecurityAuditLog]:
    rows = await db.execute(
        select(SecurityAuditLog).where(SecurityAuditLog.event_type == event.value).order_by(SecurityAuditLog.id)
    )
    return list(rows.scalars().all())


def _id(text: str) -> int:
    match = re.search(r"#(\d+) ", text)
    assert match, text
    return int(match.group(1))


async def test_create_submit_approve_pay_and_export(db_session: AsyncSession, b: Billing, keys: dict[str, str]) -> None:
    text = await _call(keys, "alice", "create", month="2026-03", note="March")
    assert text.startswith("Created a draft claim") and "2026-03 draft; 47.50 USD; 2 entries" in text
    assert "share 0.5 seat 75 marginal 0 reimbursable 37.5 USD" in text
    cid = _id(text)

    assert "(1)" in await _call(keys, "alice", "list")
    assert "#" not in (await _call(keys, "bob", "list")).split(":", 1)[1]
    assert "not found" in (await _refused(keys, "bob", "get", claim_id=cid)).lower()
    assert "administrators" in await _refused(keys, "bob", "list", user="all")

    assert "Submitted" in await _call(keys, "alice", "submit", claim_id=cid)
    assert "administrators" in (await _refused(keys, "alice", "approve", claim_id=cid)).lower()
    queue = await _call(keys, "admin", "list", user="all", status="submitted")
    assert f"#{cid} 2026-03 submitted" in queue and "holder rcm_alice" in queue
    assert "Approved" in await _call(keys, "admin", "approve", claim_id=cid)
    assert "paid" in await _call(keys, "admin", "paid", claim_id=cid)

    csv_text = await _call(keys, "alice", "export", claim_id=cid)
    assert csv_text.splitlines()[0].startswith("usage_date,project") and "total," in csv_text

    changed = await _audit(db_session, AuditEvent.AI_REIMBURSEMENT_CLAIMS_CHANGED)
    assert [(r.details["op"], r.details["holder"]) for r in changed] == [
        ("create", "rcm_alice"),
        ("submit", "rcm_alice"),
        ("approve", "rcm_alice"),
        ("paid", "rcm_alice"),
    ]
    assert all(r.details["source"] == "mcp" for r in changed)
    assert len(await _audit(db_session, AuditEvent.AI_REIMBURSEMENT_CLAIM_EXPORTED)) == 1


async def test_reject_withdraw_delete_and_refusals(b: Billing, keys: dict[str, str]) -> None:
    cid = _id(await _call(keys, "alice", "create", month="2026-03"))
    assert "Refreshed" in await _call(keys, "alice", "refresh", claim_id=cid)
    await _call(keys, "alice", "submit", claim_id=cid)
    assert "draft again" in await _call(keys, "alice", "withdraw", claim_id=cid)
    await _call(keys, "alice", "submit", claim_id=cid)
    assert "Rejected" in await _call(keys, "admin", "reject", claim_id=cid, reason="Ask again")
    assert "Reason: Ask again" in await _call(keys, "alice", "get", claim_id=cid)
    assert f"Deleted claim #{cid}" in await _call(keys, "alice", "delete", claim_id=cid)

    assert "Invalid op" in await _refused(keys, "alice", "frobnicate")
    assert "limited to specific projects" in await _refused(keys, "alice_limited", "list")
    assert "needs claim_id" in await _refused(keys, "alice", "submit")
    assert "needs month" in await _refused(keys, "alice", "create")
    assert "nothing to reimburse" in (await _refused(keys, "bob", "create", month="2026-03")).lower()


async def test_hints_and_the_setup_guide() -> None:
    hints = TOOL_ANNOTATIONS["specivo_ai_reimbursement_claims"]
    assert hints.read_only_hint is False and hints.destructive_hint is True and hints.idempotent_hint is False
    guide = generate_setup_guide()
    assert "specivo_ai_reimbursement_claims" in guide and "Claim back AI usage you paid for" in guide
