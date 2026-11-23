"""AI reimbursement claims: the REST routes and the web pages.

The rules (amounts, transitions, the snapshot rule) are
:class:`AiReimbursementClaimService`'s and are tested in
``tests/services/test_ai_reimbursement_claims``; these tests check every route
keeps them: who reaches which claim, the status codes, the CSV export, and
that a project-limited API key is refused. The world is
``test_ai_spend_billing_reports``'s with the prefix ``rci``: alice may claim
47.5 USD for March 2026.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.models.ai_reimbursement_claim import AiReimbursementClaim
from specivo.models.security_audit import SecurityAuditLog
from specivo.models.user import User
from specivo.services.api_key_service import ApiKeyService
from specivo.services.auth_service import _make_access_token
from specivo.services.security_audit_service import AuditEvent
from tests.services.test_ai_spend_billing_reports import Billing, build_billing

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

MY = "/api/v1/my/ai-reimbursement-claims/"
ADMIN = "/api/v1/admin/ai-reimbursement-claims/"
DELAY = "specivo.tasks.notifications.send_notification_email.delay"


@pytest_asyncio.fixture
async def b(db_session: AsyncSession) -> Billing:
    return await build_billing(db_session, "rci")


@pytest.fixture(autouse=True)
def mails() -> Iterator[MagicMock]:
    with patch(DELAY) as delay:
        yield delay


def _bearer(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {_make_access_token(user, get_settings())}"}


def _cookies(user: User) -> dict[str, str]:
    return {"access_token": _make_access_token(user, get_settings())}


async def _csrf(client: AsyncClient, user: User, page: str) -> str:
    resp = await client.get(page, cookies=_cookies(user))
    for key, value in resp.headers.multi_items():
        if key.lower() == "set-cookie" and value.startswith("csrf_token="):
            return value.split("=", 1)[1].split(";")[0].strip()
    raise AssertionError(f"no csrf_token cookie on {page}")


async def _post_form(client: AsyncClient, user: User, url: str, data: Mapping[str, object], page: str):
    csrf = await _csrf(client, user, page)
    return await client.post(
        url,
        data={**data, "csrf_token": csrf},
        cookies={**_cookies(user), "csrf_token": csrf},
        headers={"x-csrf-token": csrf},
        follow_redirects=False,
    )


async def _audit(db: AsyncSession, event: AuditEvent) -> list[SecurityAuditLog]:
    rows = await db.execute(
        select(SecurityAuditLog).where(SecurityAuditLog.event_type == event.value).order_by(SecurityAuditLog.id)
    )
    return list(rows.scalars().all())


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------


async def test_the_claim_flow_over_rest(client: AsyncClient, db_session: AsyncSession, b: Billing) -> None:
    w = b.w
    alice, admin, bob = _bearer(w.alice), _bearer(w.admin), _bearer(w.bob)

    resp = await client.post(MY, json={"month": "2026-03", "note": "March"}, headers=alice)
    assert resp.status_code == 201, resp.text
    claim = resp.json()
    assert claim["status"] == "draft" and claim["month"] == "2026-03" and claim["entry_count"] == 2
    assert Decimal(claim["total"]["amount"]) == Decimal("47.5") and claim["total"]["currency"] == "USD"
    assert claim["holder"]["login"] == "rci_alice" and claim["changed_entries"] == 0
    entries = {e["entry_id"]: e for e in claim["entries"]}
    assert entries[b.entries[1].id]["share"] == "0.5"
    assert Decimal(entries[b.entries[1].id]["seat_cost"]["amount"]) == 75
    assert entries[b.entries[3].id]["project_key"] == "RCIA"
    assert entries[b.entries[3].id]["billing_account"] == "Alice key"
    cid = claim["id"]

    dup = await client.post(MY, json={"month": "2026-03"}, headers=alice)
    assert dup.status_code == 409 and dup.json()["errors"][0]["code"] == "ai_reimbursement_claim_exists"
    empty = await client.post(MY, json={"month": "2026-03"}, headers=bob)
    assert empty.status_code == 422 and empty.json()["errors"][0]["code"] == "ai_reimbursement_claim_empty"
    bad = await client.post(MY, json={"month": "March"}, headers=alice)
    assert bad.status_code == 422 and bad.json()["errors"][0]["field"] == "month"

    listed = await client.get(MY, headers=alice)
    assert [c["id"] for c in listed.json()] == [cid]
    assert (await client.get(MY, headers=bob)).json() == []
    # Somebody else's claim does not exist on the personal routes, administrators included.
    for who in (bob, admin):
        assert (await client.get(f"{MY}{cid}/", headers=who)).status_code == 404
        assert (await client.post(f"{MY}{cid}/submit/", headers=who)).status_code == 404

    refreshed = await client.post(f"{MY}{cid}/refresh/", json={"note": ""}, headers=alice)
    assert refreshed.status_code == 200 and refreshed.json()["note"] is None
    submitted = await client.post(f"{MY}{cid}/submit/", headers=alice)
    assert submitted.status_code == 200 and submitted.json()["status"] == "submitted"
    assert (await client.post(f"{MY}{cid}/refresh/", headers=alice)).status_code == 409
    assert (await client.delete(f"{MY}{cid}/", headers=alice)).status_code == 409

    # Administration: people are refused; administrators see, approve and pay.
    assert (await client.get(ADMIN, headers=alice)).status_code == 403
    assert (await client.post(f"{ADMIN}{cid}/approve/", headers=alice)).status_code == 403
    queue = await client.get(ADMIN, params={"status": "submitted"}, headers=admin)
    assert queue.status_code == 200 and [c["id"] for c in queue.json()["items"]] == [cid]
    assert (await client.get(ADMIN, params={"status": "lost"}, headers=admin)).status_code == 422
    assert Decimal((await client.get(f"{ADMIN}{cid}/", headers=admin)).json()["total"]["amount"]) == Decimal("47.5")
    approved = await client.post(f"{ADMIN}{cid}/approve/", headers=admin)
    assert approved.status_code == 200 and approved.json()["decided_by"]["login"] == "rci_admin"
    again = await client.post(f"{ADMIN}{cid}/approve/", headers=admin)
    assert again.status_code == 409 and again.json()["errors"][0]["code"] == "ai_reimbursement_claim_status"
    paid = await client.post(f"{ADMIN}{cid}/paid/", headers=admin)
    assert paid.status_code == 200 and paid.json()["status"] == "paid"
    assert paid.json()["paid_by"]["login"] == "rci_admin"


async def test_reject_withdraw_and_delete_over_rest(client: AsyncClient, b: Billing) -> None:
    alice, admin = _bearer(b.w.alice), _bearer(b.w.admin)
    cid = (await client.post(MY, json={"month": "2026-03"}, headers=alice)).json()["id"]
    await client.post(f"{MY}{cid}/submit/", headers=alice)
    withdrawn = await client.post(f"{MY}{cid}/withdraw/", headers=alice)
    assert withdrawn.json()["status"] == "draft"
    await client.post(f"{MY}{cid}/submit/", headers=alice)
    rejected = await client.post(f"{ADMIN}{cid}/reject/", json={"reason": "Wrong month"}, headers=admin)
    assert rejected.status_code == 200 and rejected.json()["decision_note"] == "Wrong month"
    assert (await client.delete(f"{MY}{cid}/", headers=alice)).status_code == 204
    assert (await client.get(f"{MY}{cid}/", headers=alice)).status_code == 404


async def test_csv_export_and_who_may_have_it(client: AsyncClient, db_session: AsyncSession, b: Billing) -> None:
    alice, admin, bob = _bearer(b.w.alice), _bearer(b.w.admin), _bearer(b.w.bob)
    cid = (await client.post(MY, json={"month": "2026-03"}, headers=alice)).json()["id"]
    resp = await client.get(f"{MY}{cid}/export/", headers=alice)
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/csv")
    assert 'filename="ai-reimbursement-claim-rci_alice-2026-03.csv"' in resp.headers["content-disposition"]
    lines = resp.text.splitlines()
    assert lines[0].startswith("usage_date,project,tool,model,billing_account") and lines[-1].startswith("total,")
    assert (await client.get(f"{ADMIN}{cid}/export/", headers=admin)).text == resp.text
    assert (await client.get(f"{MY}{cid}/export/", headers=bob)).status_code == 404
    assert (await client.get(f"{ADMIN}{cid}/export/", headers=bob)).status_code == 403
    events = await _audit(db_session, AuditEvent.AI_REIMBURSEMENT_CLAIM_EXPORTED)
    assert [e.user_id for e in events] == [b.w.alice.id, b.w.admin.id]


async def test_a_project_limited_key_is_refused(client: AsyncClient, db_session: AsyncSession, b: Billing) -> None:
    _, raw = await ApiKeyService().create_key(
        db_session,
        user_id=b.w.alice.id,
        name="rci-limited",
        scopes={"projects": [b.w.p.key]},
        check_scope_access=False,
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {raw}"}
    assert (await client.get(MY, headers=headers)).status_code == 403
    assert (await client.post(MY, json={"month": "2026-03"}, headers=headers)).status_code == 403


# ---------------------------------------------------------------------------
# Web pages
# ---------------------------------------------------------------------------


async def test_the_claim_pages(client: AsyncClient, db_session: AsyncSession, b: Billing) -> None:
    w = b.w
    page = await client.get("/my/ai-reimbursement-claims/", cookies=_cookies(w.alice))
    assert page.status_code == 200 and 'data-testid="new-claim-form"' in page.text
    assert 'data-testid="claim-month"' in page.text

    created = await _post_form(
        client,
        w.alice,
        "/my/ai-reimbursement-claims/",
        {"month": "2026-03", "note": ""},
        "/my/ai-reimbursement-claims/",
    )
    assert created.status_code == 303
    claim = (
        await db_session.execute(select(AiReimbursementClaim).where(AiReimbursementClaim.user_id == w.alice.id))
    ).scalar_one()
    assert created.headers["location"].endswith(f"/my/ai-reimbursement-claims/{claim.id}/?notice=created")
    detail_url = f"/my/ai-reimbursement-claims/{claim.id}/"
    detail = await client.get(detail_url, cookies=_cookies(w.alice))
    assert detail.status_code == 200
    assert "$47.50" in detail.text and 'data-testid="claim-submit"' in detail.text
    assert detail.text.count('data-testid="claim-entry"') == 2

    # A month with nothing to claim comes back with the error.
    empty = await _post_form(
        client, w.bob, "/my/ai-reimbursement-claims/", {"month": "2026-03"}, "/my/ai-reimbursement-claims/"
    )
    assert empty.status_code == 303 and "error=ai_reimbursement_claim_empty" in empty.headers["location"]
    # Nobody else opens it.
    assert (await client.get(detail_url, cookies=_cookies(w.bob))).status_code == 404

    submitted = await _post_form(client, w.alice, f"{detail_url}submit/", {}, detail_url)
    assert submitted.status_code == 303 and "notice=submitted" in submitted.headers["location"]

    admin_list = await client.get("/admin/ai-reimbursement-claims/", cookies=_cookies(w.admin))
    assert admin_list.status_code == 200 and f'data-claim-id="{claim.id}"' in admin_list.text
    admin_url = f"/admin/ai-reimbursement-claims/{claim.id}/"
    admin_detail = await client.get(admin_url, cookies=_cookies(w.admin))
    assert 'data-testid="claim-approve"' in admin_detail.text and 'data-testid="claim-reject"' in admin_detail.text
    assert (await client.get(admin_url, cookies=_cookies(w.alice))).status_code in (302, 303, 307, 403)

    approved = await _post_form(client, w.admin, f"{admin_url}approve/", {}, admin_url)
    assert approved.status_code == 303 and "notice=approved" in approved.headers["location"]
    await db_session.refresh(claim)
    assert claim.status == "approved"
    paid_page = await client.get(admin_url, cookies=_cookies(w.admin))
    assert 'data-testid="claim-paid"' in paid_page.text

    export = await client.get(f"{detail_url}export/", cookies=_cookies(w.alice))
    assert export.status_code == 200 and export.text.startswith("usage_date,")

    menu = await client.get("/my/profile/", cookies=_cookies(w.alice))
    assert "/my/ai-reimbursement-claims/" in menu.text
