"""AI spend budgets: who sets which, the month's progress, the alerts and their audit.

The world of ``test_ai_spend_billing_reports`` with the prefix ``bsv``. Its
March 2026 spend, which the budgets measure (USD):

- ``Corp gateway`` (instance, employer): 20 (entry 5);
- ``Alice Max`` (alice's subscription account): seat cost 75 + 25 = 100;
- ``Alice key`` (alice's API key): 10 + 4 = 14;
- ``Mgr key``: 8;
- payer ``employer``: 20; payer ``employee``: 100 + 14 + 8 + 3 = 125.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import AppError, NotFoundError, PermissionDeniedError
from specivo.core.i18n import format_date
from specivo.models.ai_spend_budget import AiSpendBudget, AiSpendBudgetAlert
from specivo.models.security_audit import SecurityAuditLog
from specivo.services.ai_spend_budget_service import (
    AiSpendBudgetService,
    BudgetInput,
    parse_amount,
    parse_thresholds,
)
from specivo.services.security_audit_service import EVENT_CATEGORIES, AuditCategory, AuditEvent
from tests.services.test_ai_spend_billing_reports import Billing, build_billing
from tests.services.test_ai_spend_reports import entry, rate

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.service]

_svc = AiSpendBudgetService()
MARCH_31 = date(2026, 3, 31)
DELAY = "specivo.tasks.notifications.send_notification_email.delay"


@pytest_asyncio.fixture
async def b(db_session: AsyncSession) -> Billing:
    return await build_billing(db_session, "bsv")


@pytest.fixture
def mails() -> Iterator[MagicMock]:
    with patch(DELAY) as delay:
        yield delay


async def _audit(db: AsyncSession, event: AuditEvent) -> list[SecurityAuditLog]:
    rows = await db.execute(
        select(SecurityAuditLog).where(SecurityAuditLog.event_type == event.value).order_by(SecurityAuditLog.id)
    )
    return list(rows.scalars().all())


async def _budget(db: AsyncSession, b: Billing, *, account=None, payer=None, amount="20", **kw) -> AiSpendBudget:
    return await _svc.create(
        db,
        BudgetInput(
            billing_account_id=account.id if account is not None else None,
            payer=payer,
            amount=amount,
            currency=kw.pop("currency", "USD"),
            **kw,
        ),
        actor=b.w.admin,
    )


def _sent_to(mails: MagicMock) -> list[str]:
    return [c.args[0] for c in mails.call_args_list]


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


async def test_thresholds_and_amounts_are_validated() -> None:
    assert parse_thresholds(None) == [80, 100]
    assert parse_thresholds("") == [80, 100]
    assert parse_thresholds("100, 50 80%") == [50, 80, 100]
    assert parse_thresholds([100, 100, 120]) == [100, 120]
    for bad in ("0", "1001", "a", [1, 2, 3, 4, 5, 6], [True]):
        with pytest.raises(AppError) as exc:
            parse_thresholds(bad)
        assert exc.value.field == "thresholds", bad
    assert parse_amount("500", "USD") == Decimal(500)
    assert parse_amount("12.50", "USD") == Decimal("12.50")
    for bad in ("0", "-1", "12.505", "", "ten", 1.5, "1e20"):
        with pytest.raises(AppError) as exc:
            parse_amount(bad, "USD")
        assert exc.value.field == "amount", bad
    with pytest.raises(AppError):
        parse_amount("10.5", "JPY")


# ---------------------------------------------------------------------------
# Who sets and sees which budget
# ---------------------------------------------------------------------------


async def test_budget_crud_and_permissions(db_session: AsyncSession, b: Billing) -> None:
    db = db_session
    w = b.w
    # A person sets the budget of their own account.
    own = await _svc.create(
        db, BudgetInput(billing_account_id=b.alice_key.id, amount="50", currency="usd"), actor=w.alice
    )
    assert own.amount_currency == "USD" and own.amount_amount == 50 and own.thresholds == [80, 100]
    assert own.created_by_id == w.alice.id
    with pytest.raises(AppError) as exc:
        await _svc.create(db, BudgetInput(billing_account_id=b.alice_key.id, amount="5", currency="USD"), actor=w.alice)
    assert exc.value.code == "ai_spend_budget_exists" and exc.value.status_code == 409

    # Not somebody else's (404 whether or not it exists), not a company account, not a payer.
    with pytest.raises(NotFoundError):
        await _svc.create(db, BudgetInput(billing_account_id=b.alice_max.id, amount="5", currency="USD"), actor=w.bob)
    with pytest.raises(PermissionDeniedError):
        await _svc.create(db, BudgetInput(billing_account_id=b.gateway.id, amount="5", currency="USD"), actor=w.alice)
    with pytest.raises(PermissionDeniedError):
        await _svc.create(db, BudgetInput(payer="employer", amount="5", currency="USD"), actor=w.alice)
    # Exactly one target; a known payer; an active currency.
    with pytest.raises(AppError):
        await _svc.create(
            db,
            BudgetInput(billing_account_id=b.gateway.id, payer="employer", amount="5", currency="USD"),
            actor=w.admin,
        )
    with pytest.raises(AppError) as exc:
        await _svc.create(db, BudgetInput(payer="unknown", amount="5", currency="USD"), actor=w.admin)
    assert exc.value.field == "payer"
    with pytest.raises(AppError) as exc:
        await _svc.create(db, BudgetInput(payer="employer", amount="5", currency="XYZ"), actor=w.admin)
    assert exc.value.field == "currency"

    # Administrators set any budget.
    gateway = await _budget(db, b, account=b.gateway, amount="2000", thresholds=[50, 100])
    employer = await _budget(db, b, payer="Employer", amount="5000")
    assert employer.payer == "employer" and gateway.thresholds == [50, 100]

    # Changes: the owner changes their own, nobody else does.
    await _svc.update(db, own, {"amount": "75.5", "thresholds": "90", "is_active": False}, actor=w.alice)
    assert own.amount_amount == Decimal("75.5") and own.thresholds == [90] and own.is_active is False
    with pytest.raises(NotFoundError):
        await _svc.update(db, own, {"amount": "1"}, actor=w.bob)
    with pytest.raises(PermissionDeniedError):
        await _svc.update(db, gateway, {"amount": "1"}, actor=w.alice)
    with pytest.raises(AppError) as exc:
        await _svc.update(db, own, {"payer": "employer"}, actor=w.alice)
    assert exc.value.field == "payer"
    # A currency change re-checks the amount against it.
    with pytest.raises(AppError) as exc:
        await _svc.update(db, own, {"currency": "JPY"}, actor=w.alice)
    assert exc.value.field == "amount"

    with pytest.raises(PermissionDeniedError):
        await _svc.delete(db, employer, actor=w.alice)
    await _svc.delete(db, own, actor=w.alice)
    assert await db.get(AiSpendBudget, own.id) is None


async def test_self_service_off_leaves_budgets_to_administrators(db_session: AsyncSession, b: Billing) -> None:
    from specivo.services.ai_subscription_service import AiSubscriptionService

    db = db_session
    await AiSubscriptionService().set_self_service(db, False, actor=b.w.admin)
    with pytest.raises(PermissionDeniedError):
        await _svc.create(
            db, BudgetInput(billing_account_id=b.alice_key.id, amount="5", currency="USD"), actor=b.w.alice
        )
    budget = await _svc.create(
        db, BudgetInput(billing_account_id=b.alice_key.id, amount="5", currency="USD"), actor=b.w.admin
    )
    assert not await _svc.can_manage(db, b.w.alice, b.alice_key)
    # She still sees it.
    assert await _svc.get_visible(db, b.w.alice, budget.id) is budget


async def test_personal_budgets_are_seen_by_their_owner_and_administrators(
    db_session: AsyncSession, b: Billing
) -> None:
    db = db_session
    w = b.w
    alice = await _budget(db, b, account=b.alice_key)
    gateway = await _budget(db, b, account=b.gateway)
    employer = await _budget(db, b, payer="employer")
    mgr = await _budget(db, b, account=b.mgr_key)

    assert {x.id for x in await _svc.list_visible(db, w.alice)} == {alice.id}
    assert {x.id for x in await _svc.list_visible(db, w.bob)} == set()
    assert {x.id for x in await _svc.list_visible(db, w.admin)} == {alice.id, gateway.id, employer.id, mgr.id}
    # A company or payer budget is the administrators' even though everyone sees the company account.
    for budget in (gateway, employer, mgr):
        with pytest.raises(NotFoundError):
            await _svc.get_visible(db, w.alice, budget.id)
    with pytest.raises(NotFoundError):
        await _svc.get_visible(db, w.bob, alice.id)
    assert await _svc.get_visible(db, w.admin, alice.id) is alice


# ---------------------------------------------------------------------------
# Progress
# ---------------------------------------------------------------------------


async def test_progress_measures_the_month_per_account_and_payer(db_session: AsyncSession, b: Billing) -> None:
    db = db_session
    budgets = {
        "gateway": await _budget(db, b, account=b.gateway, amount="40"),
        "max": await _budget(db, b, account=b.alice_max, amount="80"),
        "key": await _budget(db, b, account=b.alice_key, amount="20"),
        "employer": await _budget(db, b, payer="employer", amount="1000"),
        "employee": await _budget(db, b, payer="employee", amount="250"),
    }
    progress = await _svc.progress(db, list(budgets.values()), today=MARCH_31)
    spend = {k: progress[v.id].spend.amount for k, v in budgets.items()}
    assert spend == {"gateway": 20, "max": 100, "key": 14, "employer": 20, "employee": 125}

    gw = progress[budgets["gateway"].id]
    assert gw.percent == Decimal("50.0") and gw.reached == () and not gw.over
    assert (gw.period_start, gw.period_end, gw.as_of) == (date(2026, 3, 1), MARCH_31, MARCH_31)
    seat = progress[budgets["max"].id]
    # Seat cost counts: the 100 USD plan is 125% of an 80 USD budget.
    assert seat.percent == Decimal("125.0") and seat.reached == (80, 100) and seat.over
    assert progress[budgets["key"].id].reached == ()  # 70%

    # Mid-month counts only the days so far; another month starts from nothing.
    mid = await _svc.progress(db, [budgets["key"]], today=date(2026, 3, 7))
    assert mid[budgets["key"].id].spend.amount == 10
    april = await _svc.progress(db, [budgets["key"]], today=date(2026, 4, 30))
    assert april[budgets["key"].id].spend.amount == 0 and april[budgets["key"].id].period_start == date(2026, 4, 1)


async def test_progress_is_in_the_budget_currency(db_session: AsyncSession, b: Billing) -> None:
    db = db_session
    gateway = await _budget(db, b, account=b.gateway, amount="20", currency="EUR")
    key = await _budget(db, b, account=b.alice_key, amount="20", currency="EUR")
    await rate(db, date(2026, 3, 9), "USD", "EUR", "0.5")
    await db.commit()

    progress = await _svc.progress(db, [gateway, key], today=MARCH_31)
    gw = progress[gateway.id]
    assert gw.spend.currency == "EUR" and gw.spend.amount == 10 and gw.percent == Decimal("50.0")
    assert not gw.unconverted
    # No rate for alice's days: the spend is not guessed, and the budget says so.
    k = progress[key.id]
    assert k.spend.amount == 0 and k.unconverted

    # A currency switched off since the budget was set still measures it.
    from specivo.models.currency import Currency

    eur = await db.get(Currency, "EUR")
    assert eur is not None
    eur.is_active = False
    await db.flush()
    again = await _svc.progress(db, [gateway], today=MARCH_31)
    assert again[gateway.id].spend.amount == 10


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------


async def test_a_threshold_alerts_once_per_month(db_session: AsyncSession, b: Billing, mails: MagicMock) -> None:
    db = db_session
    w = b.w
    w.alice.language = "th"
    key = await _budget(db, b, account=b.alice_key, amount="20", thresholds=[50, 100])
    gateway = await _budget(db, b, account=b.gateway, amount="20")
    await _budget(db, b, payer="employer", amount="1000")
    off = await _budget(db, b, account=b.alice_max, amount="1", is_active=False)
    await db.commit()

    outcome = await _svc.check(db, today=MARCH_31)
    assert outcome.budgets == 3
    alerts = {a.budget_id: a for a in outcome.alerts}
    assert set(alerts) == {key.id, gateway.id}
    assert alerts[key.id].thresholds == [50] and alerts[gateway.id].thresholds == [80, 100]
    # alice's account: the administrator and alice; the company account: the administrator only.
    assert {r.user_id for r in alerts[key.id].recipients} == {w.admin.id, w.alice.id}
    assert {r.user_id for r in alerts[gateway.id].recipients} == {w.admin.id}
    assert off.id not in alerts

    assert _svc.notify(outcome.alerts) == 3
    by_address = {c.args[0]: c.args for c in mails.call_args_list}
    assert set(_sent_to(mails)) == {w.admin.email, w.alice.email}
    alice_subject, alice_html = by_address[w.alice.email][1], by_address[w.alice.email][2]
    # Each in their own language: the month is Thai for alice.
    assert format_date(date(2026, 3, 1), "yMMMM", "th") in alice_subject
    assert "Alice key" in alice_subject and "50%" in alice_subject
    assert "/my/ai-billing-accounts/" in by_address[w.alice.email][3]
    assert "14" in alice_html
    gateway_mail = next(c.args for c in mails.call_args_list if "Corp gateway" in c.args[1])
    assert "100%" in gateway_mail[1] and f"/admin/ai-billing-accounts/{b.gateway.id}/" in gateway_mail[3]

    rows = (await db.execute(select(AiSpendBudgetAlert).order_by(AiSpendBudgetAlert.threshold))).scalars().all()
    assert {(r.budget_id, r.threshold) for r in rows} == {(key.id, 50), (gateway.id, 80), (gateway.id, 100)}
    assert all(r.period_start == date(2026, 3, 1) for r in rows)
    assert next(r for r in rows if r.budget_id == key.id).spend_amount == 14

    # The next run finds nothing new, so nobody is mailed again.
    mails.reset_mock()
    again = await _svc.check(db, today=MARCH_31)
    assert again.alerts == [] and _svc.notify(again.alerts) == 0 and not mails.called

    # More spend reaches the next threshold, and only that one alerts.
    await entry(
        db, w.alice, project=b.w.p, day=date(2026, 3, 20), api="7", billing_account_id=b.alice_key.id, payer="employee"
    )
    await db.commit()
    more = await _svc.check(db, today=MARCH_31)
    assert [(a.budget_id, a.thresholds) for a in more.alerts] == [(key.id, [100])]
    progress = (await _svc.progress(db, [key], today=MARCH_31))[key.id]
    assert progress.alerted == (50, 100) and progress.over

    # Raising the amount does not send the same threshold again this month.
    await _svc.update(db, key, {"amount": "10"}, actor=w.alice)
    assert (await _svc.check(db, today=MARCH_31)).alerts == []


async def test_a_new_month_starts_again(db_session: AsyncSession, b: Billing, mails: MagicMock) -> None:
    db = db_session
    gateway = await _budget(db, b, account=b.gateway, amount="20", thresholds=[80])
    await db.commit()
    assert [a.thresholds for a in (await _svc.check(db, today=MARCH_31)).alerts] == [[80]]

    # April: nothing spent yet, nothing sent.
    assert (await _svc.check(db, today=date(2026, 4, 15))).alerts == []
    await entry(
        db, b.w.bob, project=b.w.p, day=date(2026, 4, 10), api="18", payer="employer", billing_account_id=b.gateway.id
    )
    await db.commit()
    [april] = (await _svc.check(db, today=date(2026, 4, 15))).alerts
    assert april.budget_id == gateway.id and april.thresholds == [80]
    assert april.progress.period_start == date(2026, 4, 1)
    periods = await db.execute(select(AiSpendBudgetAlert.period_start).order_by(AiSpendBudgetAlert.period_start))
    assert list(periods.scalars()) == [date(2026, 3, 1), date(2026, 4, 1)]


async def test_the_first_of_a_month_also_checks_the_month_that_ended(
    db_session: AsyncSession, b: Billing, mails: MagicMock
) -> None:
    db = db_session
    mgr = await _budget(db, b, account=b.mgr_key, amount="10", thresholds=[50])
    await db.commit()
    [late] = (await _svc.check(db, today=date(2026, 4, 1))).alerts
    assert late.budget_id == mgr.id and late.progress.period_start == date(2026, 3, 1)
    assert late.progress.as_of == MARCH_31 and late.progress.spend.amount == 8


async def test_budgets_and_alerts_are_audited(db_session: AsyncSession, b: Billing, mails: MagicMock) -> None:
    db = db_session
    w = b.w
    # A person's own budget: not audited.
    own = await _svc.create(
        db, BudgetInput(billing_account_id=b.alice_key.id, amount="20", currency="USD"), actor=w.alice
    )
    await _svc.update(db, own, {"amount": "30"}, actor=w.alice)
    assert await _audit(db, AuditEvent.AI_SPEND_BUDGET_CREATED) == []
    assert await _audit(db, AuditEvent.AI_SPEND_BUDGET_UPDATED) == []

    gateway = await _budget(db, b, account=b.gateway, amount="10")
    [created] = await _audit(db, AuditEvent.AI_SPEND_BUDGET_CREATED)
    assert created.user_id == w.admin.id and created.resource_type == "ai_spend_budget"
    assert created.resource_id == gateway.id
    assert created.details["target"] == "billing_account" and created.details["owner"] == "instance"
    assert created.details["account_slug"] == "bsv-gateway" and created.details["amount"] == "10.00"
    assert created.details["thresholds"] == [80, 100]

    # An administrator changing alice's budget is audited, with what changed.
    await _svc.update(db, own, {"thresholds": [40]}, actor=w.admin)
    [updated] = await _audit(db, AuditEvent.AI_SPEND_BUDGET_UPDATED)
    assert updated.details["owner"] == w.alice.login
    assert updated.details["changes"] == {"thresholds": {"old": [80, 100], "new": [40]}}
    # Saving the same values writes nothing.
    await _svc.update(db, own, {"thresholds": "40"}, actor=w.admin)
    assert len(await _audit(db, AuditEvent.AI_SPEND_BUDGET_UPDATED)) == 1

    await db.commit()
    await _svc.check(db, today=MARCH_31)
    alerts = await _audit(db, AuditEvent.AI_SPEND_BUDGET_ALERT_SENT)
    by_budget = {a.details["budget_id"]: a for a in alerts}
    assert set(by_budget) == {own.id, gateway.id}
    gw = by_budget[gateway.id]
    assert gw.user_id is None and gw.details["month"] == "2026-03" and gw.details["thresholds"] == [80, 100]
    assert gw.details["spend"] == "20.00" and gw.details["recipients"] == 1

    payer = await _budget(db, b, payer="employee", amount="10")
    await _svc.delete(db, payer, actor=w.admin)
    [deleted] = await _audit(db, AuditEvent.AI_SPEND_BUDGET_DELETED)
    assert deleted.details["target"] == "payer" and deleted.details["payer"] == "employee"

    assert EVENT_CATEGORIES[AuditEvent.AI_SPEND_BUDGET_CREATED] is AuditCategory.ADMINISTRATION
    assert EVENT_CATEGORIES[AuditEvent.AI_SPEND_BUDGET_ALERT_SENT] is AuditCategory.AI_USAGE
    assert EVENT_CATEGORIES[AuditEvent.AI_SPEND_BUDGETS_CHANGED] is AuditCategory.AGENTS


async def test_a_mail_that_cannot_be_queued_does_not_fail_the_check(
    db_session: AsyncSession, b: Billing, mails: MagicMock
) -> None:
    db = db_session
    await _budget(db, b, account=b.gateway, amount="10")
    await db.commit()
    outcome = await _svc.check(db, today=MARCH_31)
    mails.side_effect = RuntimeError("broker down")
    assert _svc.notify(outcome.alerts) == 0
    # The alert stays recorded: a broken broker does not turn into a mail storm later.
    assert (await _svc.check(db, today=MARCH_31)).alerts == []
