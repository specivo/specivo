"""AI reimbursement claims: the monthly claim, the approval flow, the snapshot rule, the CSV export and the audit.

The world of ``test_ai_spend_billing_reports`` with the prefix ``rcs``. In
March 2026 alice may claim 47.5 USD:

- entry 1 (``RCSA``, company work): her subscription carries 75 of its 100 USD
  fee, on ``Alice Max`` with share 0.5 -> 37.5;
- entry 3 (``RCSA``): 10 USD pay per token on ``Alice key``, share 1 -> 10.

Her usage in ``RCSB`` (a personal project) is never reimbursable. The manager
may claim 2 (entry 7); bob has nothing to claim (no account with a share).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import AppError, NotFoundError, PermissionDeniedError
from specivo.core.utils import utcnow
from specivo.models.ai_reimbursement_claim import AiReimbursementClaim, AiReimbursementClaimEntry
from specivo.models.ai_usage_entry import AiUsageEntry
from specivo.models.security_audit import SecurityAuditLog
from specivo.services.ai_reimbursement_claim_service import (
    CSV_HEADER,
    AiReimbursementClaimService,
    _csv_cell,
    entry_fingerprint,
    month_end,
    parse_month,
)
from specivo.services.security_audit_service import EVENT_CATEGORIES, AuditCategory, AuditEvent
from tests.factories.user import ServiceAccountFactory
from tests.services.test_ai_spend_billing_reports import Billing, build_billing
from tests.services.test_ai_spend_reports import rate

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.service]

_svc = AiReimbursementClaimService()
MARCH = "2026-03"
DELAY = "specivo.tasks.notifications.send_notification_email.delay"


@pytest_asyncio.fixture
async def b(db_session: AsyncSession) -> Billing:
    return await build_billing(db_session, "rcs")


@pytest.fixture
def mails() -> Iterator[MagicMock]:
    with patch(DELAY) as delay:
        yield delay


async def _audit(db: AsyncSession, event: AuditEvent) -> list[SecurityAuditLog]:
    rows = await db.execute(
        select(SecurityAuditLog).where(SecurityAuditLog.event_type == event.value).order_by(SecurityAuditLog.id)
    )
    return list(rows.scalars().all())


def _by_entry(rows: list[AiReimbursementClaimEntry]) -> dict[int | None, AiReimbursementClaimEntry]:
    return {r.entry_id: r for r in rows}


async def _changes(db: AsyncSession, claim: AiReimbursementClaim) -> dict[int | None, str]:
    found = (await _svc.changes(db, [claim.id])).get(claim.id, [])
    return {c.entry_id: c.reason for c in found}


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


async def test_month_parsing() -> None:
    assert parse_month("2026-03") == date(2026, 3, 1)
    assert parse_month(" 2025-12 ") == date(2025, 12, 1)
    assert parse_month(date(2026, 3, 17)) == date(2026, 3, 1)
    for bad in ("2026-3", "2026-13", "March", "", "2026-03-01", None):
        with pytest.raises(AppError) as exc:
            parse_month(bad)
        assert exc.value.field == "month", bad
    assert month_end(date(2026, 2, 1)) == date(2026, 2, 28)


async def test_csv_cells_that_could_start_a_formula_are_quoted() -> None:
    assert _csv_cell("=SUM(A1)") == "'=SUM(A1)"
    assert _csv_cell("-1") == "'-1"
    assert _csv_cell("lab-model-1") == "lab-model-1"
    assert _csv_cell(None) == ""


# ---------------------------------------------------------------------------
# Making a claim
# ---------------------------------------------------------------------------


async def test_a_claim_freezes_the_months_reimbursable_entries(db_session: AsyncSession, b: Billing) -> None:
    db = db_session
    claim = await _svc.create(db, b.w.alice, MARCH, note="March work")
    assert claim.status == "draft" and claim.month == date(2026, 3, 1)
    assert claim.total_currency == "USD" and claim.total_amount == Decimal("47.5")
    assert claim.entry_count == 2 and claim.note == "March work"

    rows = _by_entry(await _svc.entries(db, claim))
    assert set(rows) == {b.entries[1].id, b.entries[3].id}
    sub = rows[b.entries[1].id]
    assert sub.share == Decimal("0.5") and sub.seat_amount == Decimal(75) and sub.marginal_amount == 0
    assert sub.reimbursable_amount == Decimal("37.5") and sub.billing_account_id == b.alice_max.id
    key = rows[b.entries[3].id]
    assert key.share == 1 and key.marginal_amount == 10 and key.seat_amount == 0 and key.reimbursable_amount == 10
    assert key.project_id == b.w.p.id and key.payer == "employee" and key.cost_type == "api"
    for entry_id, row in rows.items():
        live = await db.get(AiUsageEntry, entry_id)
        assert live is not None
        assert row.revision == live.revision and row.fingerprint == entry_fingerprint(live)
    assert await _changes(db, claim) == {}

    # The manager's own month; bob has nothing to claim.
    mgr = await _svc.create(db, b.w.mgr, MARCH)
    assert mgr.total_amount == 2 and mgr.entry_count == 1
    with pytest.raises(AppError) as exc:
        await _svc.create(db, b.w.bob, MARCH)
    assert exc.value.code == "ai_reimbursement_claim_empty"


async def test_what_a_claim_refuses(db_session: AsyncSession, b: Billing) -> None:
    db = db_session
    running = utcnow().date().replace(day=1)
    with pytest.raises(AppError) as exc:
        await _svc.create(db, b.w.alice, running)
    assert exc.value.code == "ai_reimbursement_claim_invalid" and exc.value.field == "month"
    with pytest.raises(AppError) as exc:
        await _svc.create(db, b.w.alice, "2026-04")
    assert exc.value.code == "ai_reimbursement_claim_empty"
    with pytest.raises(AppError) as exc:
        await _svc.create(db, b.w.alice, MARCH, note="x" * 1001)
    assert exc.value.field == "note"

    runner = ServiceAccountFactory.build(login="rcs_runner", status="active")
    db.add(runner)
    await db.flush()
    with pytest.raises(PermissionDeniedError):
        await _svc.create(db, runner, MARCH)

    await _svc.create(db, b.w.alice, MARCH)
    with pytest.raises(AppError) as exc:
        await _svc.create(db, b.w.alice, MARCH)
    assert exc.value.code == "ai_reimbursement_claim_exists" and exc.value.status_code == 409


async def test_a_claim_in_another_currency_needs_rates(db_session: AsyncSession, b: Billing) -> None:
    db = db_session
    with pytest.raises(AppError) as exc:
        await _svc.create(db, b.w.alice, MARCH, currency="EUR")
    assert exc.value.code == "ai_reimbursement_claim_unconverted"
    assert exc.value.details == {"currencies": ["USD"], "target": "EUR"}

    for day in range(1, 32):
        await rate(db, date(2026, 3, day), "USD", "EUR", "0.5")
    claim = await _svc.create(db, b.w.alice, MARCH, currency="EUR")
    assert claim.total_currency == "EUR" and claim.total_amount == Decimal("23.75")
    assert {r.currency for r in await _svc.entries(db, claim)} == {"EUR"}


# ---------------------------------------------------------------------------
# The approval flow and who may do what
# ---------------------------------------------------------------------------


async def test_submit_approve_and_pay(db_session: AsyncSession, b: Billing, mails: MagicMock) -> None:
    db = db_session
    w = b.w
    claim = await _svc.create(db, w.alice, MARCH)

    # Only the holder submits; nobody else even sees the claim.
    with pytest.raises(PermissionDeniedError):
        await _svc.submit(db, w.admin, claim)
    with pytest.raises(NotFoundError):
        await _svc.get_visible(db, w.bob, claim.id)
    assert (await _svc.get_visible(db, w.admin, claim.id)).id == claim.id

    await _svc.submit(db, w.alice, claim)
    assert claim.status == "submitted" and claim.submitted_at is not None
    # Administrators are mailed, the holder is not.
    sent = {c.args[0]: c.args for c in mails.call_args_list}
    assert w.admin.email in sent and w.alice.email not in sent
    subject = sent[w.admin.email][1]
    assert subject == "AI reimbursement claim from Rep Alice for March 2026"
    assert "47.50" in sent[w.admin.email][3]

    with pytest.raises(AppError) as exc:
        await _svc.submit(db, w.alice, claim)
    assert exc.value.code == "ai_reimbursement_claim_status" and exc.value.status_code == 409
    with pytest.raises(AppError) as exc:
        await _svc.refresh(db, w.alice, claim)
    assert exc.value.code == "ai_reimbursement_claim_status"
    with pytest.raises(AppError):
        await _svc.delete(db, w.alice, claim)

    # A person approves nothing; the holder never decides on their own claim.
    for who in (w.alice, w.mgr):
        with pytest.raises(PermissionDeniedError):
            await _svc.approve(db, who, claim)
    with pytest.raises(AppError) as exc:
        await _svc.mark_paid(db, w.admin, claim)
    assert exc.value.code == "ai_reimbursement_claim_status"

    mails.reset_mock()
    await _svc.approve(db, w.admin, claim)
    assert claim.status == "approved" and claim.decided_by_id == w.admin.id and claim.decided_at is not None
    assert [c.args[0] for c in mails.call_args_list] == [w.alice.email]
    assert "approved" in mails.call_args.args[1]
    with pytest.raises(AppError):
        await _svc.withdraw(db, w.alice, claim)

    await _svc.mark_paid(db, w.admin, claim)
    assert claim.status == "paid" and claim.paid_by_id == w.admin.id and claim.paid_at is not None
    assert claim.total_amount == Decimal("47.5")
    with pytest.raises(AppError):
        await _svc.reject(db, w.admin, claim)


async def test_withdraw_reject_and_claim_again(db_session: AsyncSession, b: Billing, mails: MagicMock) -> None:
    db = db_session
    w = b.w
    claim = await _svc.create(db, w.alice, MARCH)
    await _svc.submit(db, w.alice, claim)
    await _svc.withdraw(db, w.alice, claim)
    assert claim.status == "draft" and claim.submitted_at is None
    await _svc.submit(db, w.alice, claim)

    mails.reset_mock()
    with pytest.raises(AppError) as exc:
        await _svc.reject(db, w.admin, claim, reason="y" * 1001)
    assert exc.value.field == "reason"
    await _svc.reject(db, w.admin, claim, reason="Split the gateway month")
    assert claim.status == "rejected" and claim.decision_note == "Split the gateway month"
    [mail] = mails.call_args_list
    assert mail.args[0] == w.alice.email and "Split the gateway month" in mail.args[3]

    # A rejected claim is final; the month can be claimed again, and the rejected one deleted.
    again = await _svc.create(db, w.alice, MARCH)
    assert again.id != claim.id and again.status == "draft"
    await _svc.delete(db, w.alice, claim)
    assert await db.get(AiReimbursementClaim, claim.id) is None


async def test_administrators_never_decide_on_their_own_claim(db_session: AsyncSession, b: Billing) -> None:
    db = db_session
    w = b.w
    w.alice.is_admin = True
    await db.flush()
    claim = await _svc.create(db, w.alice, MARCH)
    await _svc.submit(db, w.alice, claim)
    assert not _svc.can_decide(w.alice, claim) and _svc.can_decide(w.admin, claim)
    for step in (_svc.approve, _svc.reject):
        with pytest.raises(PermissionDeniedError):
            await step(db, w.alice, claim)
    # They still read everyone's claims.
    rows, total = await _svc.list_claims(db, w.alice, status="submitted")
    assert total == 1 and rows[0].id == claim.id
    with pytest.raises(PermissionDeniedError):
        await _svc.list_claims(db, w.bob)


# ---------------------------------------------------------------------------
# The snapshot rule
# ---------------------------------------------------------------------------


async def test_a_submitted_claim_never_changes_and_says_what_did(
    db_session: AsyncSession, b: Billing, mails: MagicMock
) -> None:
    db = db_session
    w = b.w
    claim = await _svc.create(db, w.alice, MARCH)
    await _svc.submit(db, w.alice, claim)
    sub, key = b.entries[1], b.entries[3]

    # A comment edit bumps the revision but changes nothing that is claimed.
    key.comments = "reviewed"
    key.revision += 1
    await db.flush()
    assert await _changes(db, claim) == {}

    # Re-priced (a recompute or an edit of the cost): the entry is flagged, the claim keeps its amount.
    key.marginal_amount = Decimal(20)
    key.api_equivalent_amount = Decimal(20)
    key.revision += 1
    # The holder lowers the share of the plan account: flagged as a share change.
    b.alice_max.reimburse_share = Decimal("0.25")
    await db.flush()
    assert await _changes(db, claim) == {key.id: "billing", sub.id: "share"}

    # Re-billed to the company gateway (payer employer): still flagged as billing.
    key.payer = "employer"
    key.billing_account_id = b.gateway.id
    await db.flush()
    assert (await _changes(db, claim))[key.id] == "billing"

    # Excluded, then deleted for good.
    sub.excluded_at = datetime(2026, 4, 2, tzinfo=UTC)
    await db.flush()
    assert (await _changes(db, claim))[sub.id] == "excluded"
    key_id, sub_id = key.id, sub.id
    frozen = await _svc.entries(db, claim)
    await db.execute(delete(AiUsageEntry).where(AiUsageEntry.id == key_id))
    db.expunge(key)
    for row in frozen:
        db.expire(row)
    found = await _changes(db, claim)
    assert found == {None: "deleted", sub_id: "excluded"}
    rows = await _svc.entries(db, claim)
    assert len(rows) == 2 and {r.entry_id for r in rows} == {sub_id, None}
    assert claim.total_amount == Decimal("47.5") and claim.entry_count == 2

    # The approver still approves the frozen amount; the audit records how many entries changed.
    await _svc.approve(db, w.admin, claim)
    [event] = await _audit(db, AuditEvent.AI_REIMBURSEMENT_CLAIM_APPROVED)
    assert event.details["changed_entries"] == 2 and event.details["total"] == "47.50"


async def test_refreshing_a_draft_takes_the_current_figures(db_session: AsyncSession, b: Billing) -> None:
    db = db_session
    claim = await _svc.create(db, b.w.alice, MARCH)
    key = b.entries[3]
    key.marginal_amount = Decimal(20)
    key.api_equivalent_amount = Decimal(20)
    key.revision += 1
    await db.flush()
    assert await _changes(db, claim) == {key.id: "billing"}

    await _svc.refresh(db, b.w.alice, claim, note="Re-priced")
    assert claim.total_amount == Decimal("57.5") and claim.note == "Re-priced"
    assert await _changes(db, claim) == {}
    rows = _by_entry(await _svc.entries(db, claim))
    assert rows[key.id].revision == key.revision and rows[key.id].reimbursable_amount == 20
    with pytest.raises(PermissionDeniedError):
        await _svc.refresh(db, b.w.admin, claim)


# ---------------------------------------------------------------------------
# Export and audit
# ---------------------------------------------------------------------------


async def test_csv_export(db_session: AsyncSession, b: Billing, mails: MagicMock) -> None:
    db = db_session
    w = b.w
    claim = await _svc.create(db, w.alice, MARCH)
    await _svc.submit(db, w.alice, claim)
    b.entries[3].marginal_amount = Decimal(11)
    await db.flush()

    text = await _svc.export_csv(db, w.alice, claim)
    lines = text.splitlines()
    assert lines[0] == ",".join(CSV_HEADER)
    assert lines[1].split(",") == [
        "2026-03-05",
        "RCSA",
        "claude-code",
        "lab-model-1",
        "Alice Max",
        "employee",
        "subscription",
        "0.5",
        "0.00",
        "75.00",
        "37.50",
        "USD",
        str(b.entries[1].id),
        str(b.entries[1].revision),
        "",
    ]
    assert lines[2].endswith(f",{b.entries[3].id},{b.entries[3].revision},billing")
    assert lines[3].split(",")[0] == "total" and lines[3].split(",")[10] == "47.50"
    assert len(lines) == 4

    assert await _svc.export_csv(db, w.admin, claim) == text
    with pytest.raises(NotFoundError):
        await _svc.export_csv(db, w.bob, claim)
    events = await _audit(db, AuditEvent.AI_REIMBURSEMENT_CLAIM_EXPORTED)
    assert [e.user_id for e in events] == [w.alice.id, w.admin.id]
    assert events[0].details["format"] == "csv" and events[0].resource_type == "ai_reimbursement_claim"
    assert _svc.export_filename(claim, w.alice) == "ai-reimbursement-claim-rcs_alice-2026-03.csv"


async def test_every_step_is_audited(db_session: AsyncSession, b: Billing, mails: MagicMock) -> None:
    db = db_session
    w = b.w
    claim = await _svc.create(db, w.alice, MARCH)
    # Making and refreshing a draft are the holder's own business.
    assert await _audit(db, AuditEvent.AI_REIMBURSEMENT_CLAIM_SUBMITTED) == []
    await _svc.submit(db, w.alice, claim)
    await _svc.withdraw(db, w.alice, claim)
    await _svc.submit(db, w.alice, claim)
    await _svc.reject(db, w.admin, claim, reason="Private reason")
    other = await _svc.create(db, w.alice, MARCH)
    await _svc.submit(db, w.alice, other)
    await _svc.approve(db, w.admin, other)
    await _svc.mark_paid(db, w.admin, other)

    expected = [
        (AuditEvent.AI_REIMBURSEMENT_CLAIM_SUBMITTED, w.alice.id, ["draft", "draft", "draft"]),
        (AuditEvent.AI_REIMBURSEMENT_CLAIM_WITHDRAWN, w.alice.id, ["submitted"]),
        (AuditEvent.AI_REIMBURSEMENT_CLAIM_REJECTED, w.admin.id, ["submitted"]),
        (AuditEvent.AI_REIMBURSEMENT_CLAIM_APPROVED, w.admin.id, ["submitted"]),
        (AuditEvent.AI_REIMBURSEMENT_CLAIM_PAID, w.admin.id, ["approved"]),
    ]
    for event, actor, before in expected:
        rows = await _audit(db, event)
        assert [r.details["status_from"] for r in rows] == before, event
        assert all(r.user_id == actor and r.resource_type == "ai_reimbursement_claim" for r in rows)
        assert EVENT_CATEGORIES[event] == AuditCategory.AI_USAGE
    [rejected] = await _audit(db, AuditEvent.AI_REIMBURSEMENT_CLAIM_REJECTED)
    assert rejected.details == {
        "claim_id": claim.id,
        "holder_user_id": w.alice.id,
        "holder_login": "rcs_alice",
        "month": "2026-03",
        "currency": "USD",
        "total": "47.50",
        "entries": 2,
        "status_from": "submitted",
        "status_to": "rejected",
        "reason_given": True,
    }
    assert "Private reason" not in str(rejected.details)
    assert EVENT_CATEGORIES[AuditEvent.AI_REIMBURSEMENT_CLAIMS_CHANGED] == AuditCategory.AGENTS
