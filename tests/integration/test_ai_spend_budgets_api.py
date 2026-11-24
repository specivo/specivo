"""AI spend budgets: the REST routes and the web pages.

The rules (who sets which budget, spend, alerts) are
:class:`AiSpendBudgetService`'s and are tested in
``tests/services/test_ai_spend_budgets``; these tests check every route keeps
them: who reaches which budget, the status codes, the month's progress in the
responses and pages, and that a project-limited API key is refused. The world
is ``test_ai_spend_billing_reports``'s with the prefix ``bsa``; "today" is 31
March 2026, so the budgets measure its spend (``Alice key`` 14 USD, ``Corp
gateway`` 20 USD).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.models.ai_spend_budget import AiSpendBudget
from specivo.models.security_audit import SecurityAuditLog
from specivo.models.user import User
from specivo.services.api_key_service import ApiKeyService
from specivo.services.auth_service import _make_access_token
from specivo.services.security_audit_service import AuditEvent
from tests.services.test_ai_spend_billing_reports import Billing, build_billing

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

MY = "/api/v1/my/ai-spend-budgets/"
ADMIN = "/api/v1/admin/ai-spend-budgets/"


@pytest_asyncio.fixture
async def b(db_session: AsyncSession) -> Billing:
    return await build_billing(db_session, "bsa")


@pytest.fixture(autouse=True)
def march() -> Iterator[None]:
    """Budgets measure the running month: make it March 2026, where the world's usage is."""
    from unittest.mock import patch

    with patch("specivo.services.ai_spend_budget_service.utcnow", return_value=datetime(2026, 3, 31, 12, tzinfo=UTC)):
        yield


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


async def test_a_person_budgets_their_own_account_over_rest(client: AsyncClient, b: Billing) -> None:
    alice = _bearer(b.w.alice)
    resp = await client.post(
        MY,
        json={"billing_account_id": b.alice_key.id, "amount": "20", "currency": "USD", "thresholds": [50, 100]},
        headers=alice,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["target"] == "billing_account" and body["billing_account_slug"] == "bsa-alice-key"
    assert body["amount"] == "20" and body["currency"] == "USD" and body["thresholds"] == [50, 100]
    assert body["period_start"] == "2026-03-01" and body["spend"] == "14.00" and body["percent"] == "70.0"
    assert body["reached"] == [50] and body["alerted"] == [] and body["unconverted"] is False
    budget_id = body["id"]

    again = await client.post(
        MY, json={"billing_account_id": b.alice_key.id, "amount": "5", "currency": "USD"}, headers=alice
    )
    assert again.status_code == 409 and again.json()["errors"][0]["code"] == "ai_spend_budget_exists"
    bad = await client.post(
        MY, json={"billing_account_id": b.alice_max.id, "amount": "0", "currency": "USD"}, headers=alice
    )
    assert bad.status_code == 422 and bad.json()["errors"][0]["field"] == "amount"
    # A company account, and somebody else's, are not hers to budget here.
    for account_id in (b.gateway.id, b.mgr_key.id):
        resp = await client.post(
            MY, json={"billing_account_id": account_id, "amount": "5", "currency": "USD"}, headers=alice
        )
        assert resp.status_code == 404

    listed = await client.get(MY, headers=alice)
    assert [x["id"] for x in listed.json()] == [budget_id]
    resp = await client.patch(f"{MY}{budget_id}/", json={"amount": "14", "is_active": False}, headers=alice)
    assert resp.status_code == 200 and resp.json()["percent"] == "100.0" and resp.json()["is_active"] is False
    assert resp.json()["reached"] == [50, 100]

    # Bob neither sees nor changes it; the admin route is for administrators.
    bob = _bearer(b.w.bob)
    assert (await client.get(MY, headers=bob)).json() == []
    assert (await client.get(f"{MY}{budget_id}/", headers=bob)).status_code == 404
    assert (await client.patch(f"{MY}{budget_id}/", json={"amount": "1"}, headers=bob)).status_code == 404
    assert (await client.delete(f"{MY}{budget_id}/", headers=bob)).status_code == 404
    assert (await client.get(ADMIN, headers=alice)).status_code == 403

    assert (await client.delete(f"{MY}{budget_id}/", headers=alice)).status_code == 204
    assert (await client.get(MY, headers=alice)).json() == []


async def test_administrators_budget_accounts_and_payers(
    client: AsyncClient, db_session: AsyncSession, b: Billing
) -> None:
    admin = _bearer(b.w.admin)
    gateway = await client.post(
        ADMIN, json={"billing_account_id": b.gateway.id, "amount": "25", "currency": "USD"}, headers=admin
    )
    assert gateway.status_code == 201 and gateway.json()["spend"] == "20.00" and gateway.json()["reached"] == [80]
    employer = await client.post(ADMIN, json={"payer": "employer", "amount": "100", "currency": "USD"}, headers=admin)
    assert employer.status_code == 201
    assert employer.json()["target"] == "payer" and employer.json()["spend"] == "20.00"
    alice = await client.post(
        ADMIN, json={"billing_account_id": b.alice_key.id, "amount": "10", "currency": "USD"}, headers=admin
    )
    assert alice.status_code == 201 and alice.json()["owner_user_id"] == b.w.alice.id
    both = await client.post(
        ADMIN,
        json={"billing_account_id": b.gateway.id, "payer": "employee", "amount": "1", "currency": "USD"},
        headers=admin,
    )
    assert both.status_code == 422

    listed = (await client.get(ADMIN, headers=admin)).json()
    assert listed["total_count"] == 3
    # Payer budgets first.
    assert listed["items"][0]["payer"] == "employer"

    resp = await client.patch(f"{ADMIN}{employer.json()['id']}/", json={"thresholds": [10]}, headers=admin)
    assert resp.status_code == 200 and resp.json()["reached"] == [10]
    assert (await client.delete(f"{ADMIN}{gateway.json()['id']}/", headers=admin)).status_code == 204
    events = {e.event_type for e in await _audit(db_session, AuditEvent.AI_SPEND_BUDGET_CREATED)}
    assert events == {AuditEvent.AI_SPEND_BUDGET_CREATED.value}
    assert len(await _audit(db_session, AuditEvent.AI_SPEND_BUDGET_CREATED)) == 3
    assert len(await _audit(db_session, AuditEvent.AI_SPEND_BUDGET_DELETED)) == 1

    # alice sees her own account's budget, set by an administrator, and nothing else.
    mine = (await client.get(MY, headers=_bearer(b.w.alice))).json()
    assert [x["billing_account_id"] for x in mine] == [b.alice_key.id]


async def test_a_project_limited_key_is_refused(client: AsyncClient, db_session: AsyncSession, b: Billing) -> None:
    _, raw = await ApiKeyService().create_key(
        db_session,
        user_id=b.w.alice.id,
        name="bsa-limited",
        scopes={"projects": [b.w.p.key]},
        check_scope_access=False,
    )
    await db_session.commit()
    resp = await client.get(MY, headers={"Authorization": f"Bearer {raw}"})
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Web pages
# ---------------------------------------------------------------------------


async def test_the_account_page_sets_and_shows_a_budget(
    client: AsyncClient, db_session: AsyncSession, b: Billing
) -> None:
    page = f"/my/ai-billing-accounts/{b.alice_key.id}/"
    resp = await client.get(page, cookies=_cookies(b.w.alice))
    assert resp.status_code == 200
    assert 'data-testid="billing-account-budget-card"' in resp.text and 'data-testid="budget-progress"' not in resp.text

    bad = await _post_form(
        client, b.w.alice, f"{page}budget/", {"amount": "12.345", "currency": "USD", "thresholds": "80"}, page
    )
    assert bad.status_code == 422 and 'data-testid="budget-error"' in bad.text and "12.345" in bad.text

    ok = await _post_form(
        client,
        b.w.alice,
        f"{page}budget/",
        {"amount": "20", "currency": "USD", "thresholds": "50, 100", "is_active": "1"},
        page,
    )
    assert ok.status_code == 303 and ok.headers["location"].endswith("notice=budget_saved")
    budget = (await db_session.execute(select(AiSpendBudget))).scalar_one()
    assert budget.billing_account_id == b.alice_key.id and budget.thresholds == [50, 100]

    shown = await client.get(page, cookies=_cookies(b.w.alice))
    assert 'data-testid="budget-progress"' in shown.text and 'data-percent="70.0"' in shown.text
    listing = await client.get("/my/ai-billing-accounts/", cookies=_cookies(b.w.alice))
    assert 'data-percent="70.0"' in listing.text

    # Bob cannot reach alice's account page or its budget form.
    assert (await client.get(page, cookies=_cookies(b.w.bob))).status_code == 404
    other = await _post_form(
        client, b.w.bob, f"{page}budget/", {"amount": "1", "currency": "USD"}, "/my/ai-billing-accounts/"
    )
    assert other.status_code == 404

    removed = await _post_form(client, b.w.alice, f"{page}budget/delete/", {}, page)
    assert removed.status_code == 303 and (await db_session.execute(select(AiSpendBudget))).first() is None


async def test_the_admin_pages_show_payer_budgets_and_the_report_panel(
    client: AsyncClient, db_session: AsyncSession, b: Billing
) -> None:
    admin = b.w.admin
    page = "/admin/ai-billing-accounts/"
    resp = await _post_form(
        client,
        admin,
        "/admin/ai-spend-budgets/payer/employer/",
        {"amount": "40", "currency": "USD", "thresholds": "80, 100", "is_active": "1"},
        page,
    )
    assert resp.status_code == 303
    resp = await _post_form(
        client,
        admin,
        f"/admin/ai-billing-accounts/{b.gateway.id}/budget/",
        {"amount": "20", "currency": "USD", "is_active": "1"},
        page,
    )
    assert resp.status_code == 303

    shown = await client.get(page, cookies=_cookies(admin))
    assert 'data-testid="payer-budgets-card"' in shown.text and 'data-percent="50.0"' in shown.text
    assert 'data-percent="100.0"' in shown.text
    bad = await _post_form(
        client, admin, "/admin/ai-spend-budgets/payer/employee/", {"amount": "", "currency": "USD"}, page
    )
    assert bad.status_code == 422 and 'data-testid="budget-error"' in bad.text
    missing = await _post_form(client, admin, "/admin/ai-spend-budgets/payer/unknown/", {"amount": "1"}, page)
    assert missing.status_code == 404
    # People do not reach the payer budgets.
    refused = await _post_form(
        client,
        b.w.alice,
        "/admin/ai-spend-budgets/payer/employer/",
        {"amount": "1", "currency": "USD"},
        "/my/ai-billing-accounts/",
    )
    assert refused.status_code in (403, 404)

    report = await client.get("/admin/ai-spend/", cookies=_cookies(admin))
    assert report.status_code == 200
    assert 'data-testid="spend-budgets"' in report.text and report.text.count('data-testid="spend-budget-row"') == 2
    # A drill step (htmx) does not rebuild the panel.
    partial = await client.get("/admin/ai-spend/", cookies=_cookies(admin), headers={"HX-Request": "true"})
    assert 'data-testid="spend-budgets"' not in partial.text
