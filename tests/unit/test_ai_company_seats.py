"""Company seat plans, the pure rules: one seat's share of a fee, members by date, vacant seats, seat limits."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from specivo.core.ai_subscriptions import (
    PlainMembership,
    PlainSubscription,
    member_charges,
    member_span,
    most_members_at_once,
    prorated_fee,
    vacant_charges,
)

pytestmark = pytest.mark.unit


def _plan(amount: str = "300", period: str = "month", starts: date = date(2026, 1, 1), ends: date | None = None):
    return PlainSubscription(
        id=7,
        user_id=None,
        provider="anthropic",
        amount=Decimal(amount),
        currency_code="USD",
        billing_period=period,
        starts_on=starts,
        ends_on=ends,
    )


def test_one_seat_of_a_whole_month_is_the_fee_divided_by_the_seats() -> None:
    assert prorated_fee(Decimal(300), "month", date(2026, 1, 1), None, 2026, 4, seats=10) == (Decimal(30), 30)
    # A yearly fee: a twelfth, then a seat (one division).
    assert prorated_fee(Decimal(3600), "year", date(2026, 1, 1), None, 2026, 4, seats=10) == (Decimal(30), 30)
    # Part of a month: the days held, of the whole month's seat.
    fee, days = prorated_fee(Decimal(300), "month", date(2026, 4, 16), None, 2026, 4, seats=10)
    assert (fee, days) == (Decimal(15), 15)
    # One seat is the plain rule.
    assert prorated_fee(Decimal(20), "month", date(2026, 1, 1), None, 2026, 3) == (Decimal(20), 31)


def test_a_plan_has_at_least_one_seat() -> None:
    with pytest.raises(ValueError):
        prorated_fee(Decimal(10), "month", date(2026, 1, 1), None, 2026, 1, seats=0)


def test_a_member_is_covered_on_the_days_both_the_plan_and_the_seat_run() -> None:
    plan = _plan(starts=date(2026, 3, 1), ends=date(2026, 6, 30))
    assert member_span(plan, PlainMembership(1, date(2026, 2, 1), None)) == (date(2026, 3, 1), date(2026, 6, 30))
    assert member_span(plan, PlainMembership(1, date(2026, 4, 10), date(2026, 5, 5))) == (
        date(2026, 4, 10),
        date(2026, 5, 5),
    )
    assert member_span(plan, PlainMembership(1, date(2026, 7, 1), None)) is None
    open_plan = _plan(starts=date(2026, 3, 1))
    assert member_span(open_plan, PlainMembership(1, date(2026, 1, 1), None)) == (date(2026, 3, 1), None)


def test_each_member_carries_one_seat_by_the_days_they_hold_it() -> None:
    plan = _plan("300")
    members = [
        PlainMembership(1, date(2026, 1, 1), None),
        # Leaves on 15 April: half of April.
        PlainMembership(2, date(2026, 1, 1), date(2026, 4, 15)),
        # Joins on 16 April.
        PlainMembership(3, date(2026, 4, 16), None),
    ]
    charges = member_charges(plan, 10, members, date(2026, 3, 1), date(2026, 4, 30))
    by = {(c.user_id, c.month): c.amount.amount for c in charges}
    assert by == {
        (1, 3): Decimal(30),
        (1, 4): Decimal(30),
        (2, 3): Decimal(30),
        (2, 4): Decimal(15),
        (3, 4): Decimal(15),
    }
    assert {c.subscription_id for c in charges} == {7}
    assert all(c.amount.currency == "USD" for c in charges)


def test_vacant_seats_are_the_seat_days_nobody_held() -> None:
    plan = _plan("300")
    members = [PlainMembership(1, date(2026, 1, 1), None), PlainMembership(2, date(2026, 4, 16), None)]
    # April: 10 seats x 30 days = 300 seat-days; held 30 + 15 = 45; vacant 255 -> 255 USD.
    [april] = vacant_charges(plan, 10, members, date(2026, 4, 1), date(2026, 4, 30))
    assert april.user_id is None and april.amount.amount == Decimal(255)
    held = sum(c.amount.amount for c in member_charges(plan, 10, members, date(2026, 4, 1), date(2026, 4, 30)))
    assert held + april.amount.amount == Decimal(300)


def test_a_full_plan_leaves_exactly_nothing_vacant() -> None:
    plan = _plan("100")
    members = [PlainMembership(i, date(2026, 1, 1), None) for i in range(3)]
    assert vacant_charges(plan, 3, members, date(2026, 3, 1), date(2026, 3, 31)) == []


def test_a_zero_fee_plan_costs_nothing() -> None:
    plan = _plan("0")
    members = [PlainMembership(1, date(2026, 1, 1), None)]
    [charge] = member_charges(plan, 5, members, date(2026, 3, 1), date(2026, 3, 31))
    assert charge.amount.amount == 0
    assert all(c.amount.amount == 0 for c in vacant_charges(plan, 5, members, date(2026, 3, 1), date(2026, 3, 31)))


def test_most_members_at_once() -> None:
    assert most_members_at_once([]) == 0
    periods = [
        (date(2026, 1, 1), date(2026, 1, 31)),
        (date(2026, 2, 1), None),  # starts the day after the first ends: the same seat
        (date(2026, 1, 15), date(2026, 2, 10)),
    ]
    assert most_members_at_once(periods) == 2
    assert most_members_at_once([(date(2026, 1, 1), date(2026, 1, 31)), (date(2026, 1, 31), None)]) == 2
