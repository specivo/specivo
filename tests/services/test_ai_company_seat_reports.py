"""Seat cost of company seat plans in spend reports: one seat per member, vacant seats, the payer, never reimbursable.

The world (prefix ``cs``): ``CSA`` is company work (payer default ``employer``).
The company plan *Team* costs 300 USD a month for 10 seats (30 USD a seat) and
is paid through its employer account. In March 2026 (31 days):

- ``alice`` holds a seat all month and logs two entries on the plan, worth 30
  and 10 USD at API prices: she carries 30 USD, 22.50 and 7.50;
- ``bob`` holds a seat from 1 to 15 March and logs one entry worth 20: he
  carries 30 x 15 / 31;
- ``carol`` holds a seat from 16 March and logs nothing: her 30 x 16 / 31 is
  unallocated;
- the other seats: 310 seat-days less 62 held = 248, so 300 x 248 / 310 = 240
  USD unallocated, on a row with nobody.

Alice also pays 10 USD through her own API key (reimbursable in full) in CSA.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.ai_billing_account import AiBillingAccount
from specivo.models.ai_subscription import AiSubscription
from specivo.models.project import Project
from specivo.models.user import User
from specivo.services.ai_spend_report_service import ReportFilters, SpendReport
from specivo.services.ai_subscription_service import AiSubscriptionService, SubscriptionInput
from specivo.services.permission_service import clear_role_cache
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, UserFactory
from tests.services.test_ai_spend_reports import amount, entry, run

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.service]

_service = AiSubscriptionService()
CENT = Decimal("0.01")


@dataclass
class Seats:
    admin: User
    alice: User
    bob: User
    carol: User
    p: Project
    plan: AiSubscription
    account: AiBillingAccount
    alice_key: AiBillingAccount


def _close(value: Decimal, expected: Decimal) -> bool:
    return abs(value - expected) < CENT


@pytest_asyncio.fixture
async def s(db_session: AsyncSession) -> Seats:
    db = db_session
    admin: User = AdminUserFactory.build(login="cs_admin", status="active")
    alice: User = UserFactory.build(login="cs_alice", status="active", display_name="Seat Alice")
    bob: User = UserFactory.build(login="cs_bob", status="active", display_name="Seat Bob")
    carol: User = UserFactory.build(login="cs_carol", status="active", display_name="Seat Carol")
    p: Project = ProjectFactory.build(
        key="CSA", identifier="csa", is_public=False, settings={"ai_usage_payer_default": "employer"}
    )
    db.add_all([admin, alice, bob, carol, p])
    await db.flush()
    plan = await _service.create(
        db,
        SubscriptionInput(
            plan_key="custom",
            provider="testlab",
            plan_name="Team",
            tool_slugs=["claude-code"],
            amount="300",
            currency="USD",
            starts_on=date(2026, 1, 1),
            seat_count=10,
        ),
        owner=None,
        actor=admin,
    )
    for person, starts, ends in (
        (alice, date(2026, 3, 1), None),
        (bob, date(2026, 3, 1), date(2026, 3, 15)),
        (carol, date(2026, 3, 16), None),
    ):
        await _service.add_member(db, plan, person, starts_on=starts, ends_on=ends, actor=admin)
    account = await db.get(AiBillingAccount, plan.billing_account_id)
    assert account is not None
    alice_key = AiBillingAccount(
        slug="cs-alice-key",
        name="Alice key",
        kind="api_key",
        owner_user_id=alice.id,
        payer="employee",
        reimburse_share=Decimal(1),
    )
    db.add(alice_key)
    await db.flush()

    company = {
        "cost_type": "subscription",
        "subscription": plan,
        "payer": "employer",
        "billing_account_id": account.id,
        "billing_method": "subscription",
    }
    await entry(db, alice, project=p, day=date(2026, 3, 5), api="30", **company)
    await entry(db, alice, project=p, day=date(2026, 3, 20), api="10", **company)
    await entry(db, bob, project=p, day=date(2026, 3, 10), api="20", **company)
    await entry(db, alice, project=p, day=date(2026, 3, 7), api="10", payer="employee", billing_account_id=alice_key.id)
    await db.commit()
    clear_role_cache(db)
    return Seats(admin, alice, bob, carol, p, plan, account, alice_key)


def _rows(report: SpendReport, **key: object) -> list:
    return [r for r in report.rows if all(r.key.get(k) == v for k, v in key.items())]


def _seat(rows: list, measure: str) -> Decimal:
    return sum((getattr(r, measure).total.amount for r in rows if getattr(r, measure) is not None), Decimal(0))


async def test_each_member_carries_one_seat_and_the_vacant_seats_are_nobodys(
    db_session: AsyncSession, s: Seats
) -> None:
    report = await run(db_session, s.admin, group_by=["user"])
    assert report.seat_cost_shown and report.unallocated_shown

    alice, bob, carol = _rows(report, user=s.alice.id), _rows(report, user=s.bob.id), _rows(report, user=s.carol.id)
    assert _seat(alice, "seat_allocated") == Decimal(30)
    assert _close(_seat(bob, "seat_allocated"), Decimal(30) * 15 / 31)
    assert _seat(carol, "seat_allocated") == 0
    assert _close(_seat(carol, "seat_unallocated"), Decimal(30) * 16 / 31)
    nobody = _rows(report, user=None)
    assert _close(_seat(nobody, "seat_unallocated"), Decimal(240))
    # The whole month's fee, and nothing more.
    assert _close(amount(report.totals.seat_allocated) + amount(report.totals.seat_unallocated), Decimal(300))


async def test_a_members_seat_is_spread_over_their_own_usage(db_session: AsyncSession, s: Seats) -> None:
    report = await run(db_session, s.admin, s.p, group_by=["user"], period="day")
    # Alice's two entries carry 22.50 and 7.50 of her seat, by their API-equivalent value.
    shares = sorted(amount(r.seat_allocated) for r in _rows(report, user=s.alice.id) if r.seat_allocated is not None)
    assert [s for s in shares if s] == [Decimal("7.5"), Decimal("22.5")]


async def test_company_seat_cost_is_the_employers(db_session: AsyncSession, s: Seats) -> None:
    report = await run(db_session, s.admin, group_by=["payer"])
    payers = {r.key["payer"]: r for r in report.payer_totals}
    employer = payers["employer"]
    assert _close(amount(employer.seat_allocated) + amount(employer.seat_unallocated), Decimal(300))
    assert payers["employee"].seat_allocated is None or amount(payers["employee"].seat_allocated) == 0


async def test_company_seat_cost_is_never_reimbursable(db_session: AsyncSession, s: Seats) -> None:
    report = await run(db_session, s.admin, group_by=["user"])
    assert report.reimbursable_shown
    # Only alice's own key: 10 USD. Her 30 USD of company seat cost does not count.
    assert amount(report.totals.reimbursable) == Decimal(10)
    assert sum(amount(r.reimbursable) for r in _rows(report, user=s.alice.id) if r.reimbursable) == Decimal(10)
    payers = {r.key["payer"]: r for r in report.payer_totals}
    assert amount(payers["employer"].reimbursable) == 0


async def test_a_person_filter_shows_their_seat_and_no_vacant_seats(db_session: AsyncSession, s: Seats) -> None:
    carol = await run(db_session, s.admin, group_by=["user"], filters=ReportFilters(user_id=s.carol.id))
    assert {r.key.get("user") for r in carol.rows} == {s.carol.id}
    assert _close(amount(carol.totals.seat_unallocated), Decimal(30) * 16 / 31)
    bob = await run(db_session, s.admin, filters=ReportFilters(user_id=s.bob.id))
    assert _close(amount(bob.totals.seat_allocated), Decimal(30) * 15 / 31)
    assert bob.totals.seat_unallocated is None or amount(bob.totals.seat_unallocated) == 0


async def test_seat_charges_between(db_session: AsyncSession, s: Seats) -> None:
    db = db_session
    charges = await _service.seat_charges_between(db, date(2026, 3, 1), date(2026, 3, 31))
    by = {c.user_id: c for c in charges if c.subscription_id == s.plan.id}
    assert by[s.alice.id].amount.amount == Decimal(30)
    assert (by[s.bob.id].active_days, by[s.carol.id].active_days) == (15, 16)
    assert by[None].amount.amount == Decimal(240)
    assert _close(sum(c.amount.amount for c in by.values()), Decimal(300))
    # One person: their seats only, never the vacant ones.
    [bob] = await _service.seat_cost(db, s.bob.id, 2026, 3)
    assert (bob.subscription_id, bob.user_id, bob.active_days) == (s.plan.id, s.bob.id, 15)
    assert await _service.seat_cost(db, s.bob.id, 2026, 4) == []
