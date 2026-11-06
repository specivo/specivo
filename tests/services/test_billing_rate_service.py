"""Billing rates on the money convention, and the billable report across currencies.

The report keeps one exact subtotal per rate currency, converts each entry on
its own date into the reporting currency through the exchange rate service,
lists amounts without a usable exchange rate as unconverted and entries
without an effective billing rate as unrated, and loads everything with a
query count that does not grow with the number of entries.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import event, update
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import AppError
from specivo.core.exchange import MissingReason, RatePolicy
from specivo.core.money import Money
from specivo.models.billing_rate import BillingRate
from specivo.models.currency import Currency
from specivo.models.exchange_rate import ExchangeRate
from specivo.models.project import Project
from specivo.models.time_entry import TimeEntry, TimeEntryActivity
from specivo.models.user import User
from specivo.schemas.billing_rate import BillingRateCreate, BillingRateOut, BillingRateUpdate, BillingReportOut
from specivo.schemas.money import MoneyIn
from specivo.services.billing_rate_service import BillingRateService
from tests.factories.project import ProjectFactory
from tests.factories.time_entry import TimeEntryActivityFactory, TimeEntryFactory
from tests.factories.user import AdminUserFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.service]

_service = BillingRateService()
SPAN = (date(2026, 10, 1), date(2026, 10, 31))
# Fixed so entries resolve against stored rates whatever the instance's max age setting says.
POLICY = RatePolicy.entry_date(7)


class World:
    def __init__(self, project: Project, admin: User, alice: User, bob: User, activity: TimeEntryActivity) -> None:
        self.project = project
        self.admin = admin
        self.alice = alice
        self.bob = bob
        self.activity = activity


@pytest_asyncio.fixture
async def world(db_session: AsyncSession) -> World:
    project = ProjectFactory.build(key="BRSVC", identifier="brsvc")
    activity = TimeEntryActivityFactory.build(name="Brsvc Development")
    admin = AdminUserFactory.build(login="brsvc_admin", status="active")
    alice = UserFactory.build(login="brsvc_alice", status="active")
    bob = UserFactory.build(login="brsvc_bob", status="active")
    db_session.add_all([project, activity, admin, alice, bob])
    await db_session.flush()
    return World(project, admin, alice, bob, activity)


def _rate(w: World, amount: str, currency: str, start: date, user: User | None = None) -> BillingRate:
    return BillingRate(
        project_id=w.project.id,
        user_id=user.id if user else None,
        hourly_rate=Decimal(amount),
        currency_code=currency,
        effective_from=start,
    )


def _entry(w: World, user: User, hours: str, day: date, billable: bool = True) -> TimeEntry:
    return TimeEntryFactory.build(
        project_id=w.project.id,
        user_id=user.id,
        activity_id=w.activity.id,
        hours=Decimal(hours),
        spent_on=day,
        is_billable=billable,
    )


def _fx(day: date, base: str, quote: str, rate: str) -> ExchangeRate:
    return ExchangeRate(rate_date=day, base_code=base, quote_code=quote, rate=Decimal(rate), source="manual")


async def _add(db: AsyncSession, *rows: object) -> list:
    db.add_all(rows)
    await db.flush()
    return list(rows)


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


async def test_two_currencies_in_one_project_are_subtotalled_and_converted(
    db_session: AsyncSession, world: World
) -> None:
    w = world
    await _add(
        db_session,
        _rate(w, "50.00", "USD", date(2026, 1, 1)),
        _rate(w, "1500.00", "THB", date(2026, 1, 1), user=w.alice),
        _fx(date(2026, 10, 1), "THB", "USD", "0.025"),
    )
    alice_1, alice_2, bob_1, _ = await _add(
        db_session,
        _entry(w, w.alice, "2.00", date(2026, 10, 2)),
        _entry(w, w.alice, "1.50", date(2026, 10, 3)),
        _entry(w, w.bob, "3.00", date(2026, 10, 2)),
        _entry(w, w.bob, "9.00", date(2026, 10, 2), billable=False),
    )

    report = await _service.billable_report(db_session, w.project, w.admin, *SPAN, policy=POLICY)

    assert report.currency == "USD"
    assert [(e.time_entry_id, e.amount) for e in report.entries] == [
        (alice_1.id, Money("3000", "THB")),
        (bob_1.id, Money("150", "USD")),
        (alice_2.id, Money("2250", "THB")),
    ]
    assert [(s.currency, s.hours, s.entry_count, s.amount) for s in report.subtotals] == [
        ("THB", Decimal("3.50"), 2, Money("5250", "THB")),
        ("USD", Decimal("3.00"), 1, Money("150", "USD")),
    ]
    # 5250 THB at 0.025 is 131.25 USD, plus 150 USD.
    assert report.total.total == Money("281.25", "USD")
    assert report.total.converted_count == 3
    assert report.total.unconverted == ()
    assert [(r.from_currency, r.rate_date) for r in report.total.rates_used] == [("THB", date(2026, 10, 1))]
    assert report.rated_hours == Decimal("6.50")
    assert report.unrated == ()

    out = BillingReportOut.from_report(report)
    assert Decimal(out.total.total.amount) == Decimal("281.25")
    assert [(s.currency, Decimal(s.amount.amount)) for s in out.subtotals] == [
        ("THB", Decimal("5250")),
        ("USD", Decimal("150")),
    ]


async def test_an_entry_without_an_effective_rate_is_listed_as_unrated(db_session: AsyncSession, world: World) -> None:
    w = world
    await _add(db_session, _rate(w, "40.00", "USD", date(2026, 10, 10), user=w.alice))
    early, late, bobs = await _add(
        db_session,
        _entry(w, w.alice, "2.00", date(2026, 10, 5)),
        _entry(w, w.alice, "1.00", date(2026, 10, 12)),
        _entry(w, w.bob, "4.00", date(2026, 10, 12)),
    )

    report = await _service.billable_report(db_session, w.project, w.admin, *SPAN, policy=POLICY)

    assert [e.time_entry_id for e in report.entries] == [late.id]
    assert [(u.time_entry_id, u.hours) for u in report.unrated] == [
        (early.id, Decimal("2.00")),
        (bobs.id, Decimal("4.00")),
    ]
    assert report.unrated_hours == Decimal("6.00")
    assert report.rated_hours == Decimal("1.00")
    assert report.total.total == Money("40", "USD")


async def test_a_missing_exchange_rate_is_listed_as_unconverted(db_session: AsyncSession, world: World) -> None:
    w = world
    await _add(
        db_session,
        _rate(w, "60.00", "USD", date(2026, 1, 1)),
        _rate(w, "45.00", "EUR", date(2026, 1, 1), user=w.bob),
    )
    await _add(
        db_session,
        _entry(w, w.alice, "2.00", date(2026, 10, 2)),
        _entry(w, w.bob, "3.00", date(2026, 10, 2)),
        _entry(w, w.bob, "1.00", date(2026, 10, 9)),
    )

    report = await _service.billable_report(db_session, w.project, w.admin, *SPAN, policy=POLICY)

    assert report.total.total == Money("120", "USD")
    assert report.total.converted_count == 1
    assert [(g.currency, g.reason, g.amount, g.count) for g in report.total.unconverted] == [
        ("EUR", MissingReason.NO_RATE, Money("180", "EUR"), 2)
    ]
    assert [(s.currency, s.amount) for s in report.subtotals] == [
        ("EUR", Money("180", "EUR")),
        ("USD", Money("120", "USD")),
    ]
    out = BillingReportOut.from_report(report)
    assert [(g.currency, g.reason, Decimal(g.amount.amount)) for g in out.total.unconverted] == [
        ("EUR", "no_rate", Decimal("180"))
    ]


async def test_the_project_reporting_currency_is_the_target(db_session: AsyncSession, world: World) -> None:
    w = world
    w.project.reporting_currency_code = "THB"
    await _add(
        db_session,
        _rate(w, "10.00", "USD", date(2026, 1, 1)),
        _fx(date(2026, 10, 1), "USD", "THB", "36.5"),
    )
    await _add(db_session, _entry(w, w.alice, "2.00", date(2026, 10, 2)))

    report = await _service.billable_report(db_session, w.project, w.admin, *SPAN, policy=POLICY)
    assert report.currency == "THB"
    assert report.total.total == Money("730", "THB")

    in_usd = await _service.billable_report(db_session, w.project, w.admin, *SPAN, currency="USD", policy=POLICY)
    assert in_usd.total.total == Money("20", "USD")


async def test_the_rate_in_effect_on_each_entry_date_applies(db_session: AsyncSession, world: World) -> None:
    w = world
    old, new, alices = await _add(
        db_session,
        _rate(w, "100", "USD", date(2026, 1, 1)),
        _rate(w, "120", "USD", date(2026, 10, 15)),
        _rate(w, "90", "USD", date(2026, 10, 20), user=w.alice),
    )
    await _add(
        db_session,
        _entry(w, w.bob, "1.00", date(2026, 10, 14)),
        _entry(w, w.bob, "1.00", date(2026, 10, 15)),
        _entry(w, w.alice, "1.00", date(2026, 10, 16)),
        _entry(w, w.alice, "1.00", date(2026, 10, 21)),
    )

    report = await _service.billable_report(db_session, w.project, w.admin, *SPAN, policy=POLICY)

    assert [e.rate_id for e in report.entries] == [old.id, new.id, new.id, alices.id]
    assert await _service.get_effective_rate(db_session, w.alice.id, w.project.id, date(2026, 10, 21)) is alices
    assert await _service.get_effective_rate(db_session, w.bob.id, w.project.id, date(2026, 10, 21)) is new
    assert await _service.get_effective_rate(db_session, w.bob.id, w.project.id, date(2025, 12, 31)) is None


async def test_amounts_keep_full_precision_for_zero_and_three_decimal_currencies(
    db_session: AsyncSession, world: World
) -> None:
    w = world
    await _add(
        db_session,
        _rate(w, "1500", "JPY", date(2026, 1, 1), user=w.alice),
        _rate(w, "12.125", "BHD", date(2026, 1, 1), user=w.bob),
    )
    await _add(
        db_session,
        _entry(w, w.alice, "1.25", date(2026, 10, 2)),
        _entry(w, w.bob, "0.75", date(2026, 10, 2)),
    )

    report = await _service.billable_report(db_session, w.project, w.admin, *SPAN, policy=POLICY)

    assert [(s.currency, s.amount) for s in report.subtotals] == [
        ("BHD", Money("9.09375", "BHD")),
        ("JPY", Money("1875", "JPY")),
    ]


async def test_the_query_count_does_not_grow_with_the_entries(db_session: AsyncSession, world: World) -> None:
    w = world
    extra = [UserFactory.build(login=f"brsvc_extra_{n}", status="active") for n in range(8)]
    await _add(db_session, *extra)
    await _add(
        db_session,
        _rate(w, "50.00", "USD", date(2026, 1, 1)),
        *[_rate(w, "1000.00", "THB", date(2026, 1, 1), user=u) for u in extra[::2]],
        *[_rate(w, "40.00", "EUR", date(2026, 1, 1), user=u) for u in extra[1::2]],
        _fx(date(2026, 10, 1), "THB", "USD", "0.025"),
        _fx(date(2026, 10, 1), "EUR", "USD", "1.1"),
    )

    async def statements() -> int:
        seen: list[str] = []

        def _count(conn, cursor, statement, *args) -> None:  # noqa: ANN001
            seen.append(statement)

        engine = db_session.bind.sync_engine  # type: ignore[union-attr]
        event.listen(engine, "before_cursor_execute", _count)
        try:
            await _service.billable_report(db_session, w.project, w.admin, *SPAN, policy=POLICY)
        finally:
            event.remove(engine, "before_cursor_execute", _count)
        return len(seen)

    await _add(db_session, _entry(w, extra[0], "1.00", date(2026, 10, 2)))
    few = await statements()

    await _add(
        db_session,
        *[_entry(w, u, "1.00", date(2026, 10, day)) for u in [*extra, w.alice, w.bob] for day in range(2, 8)],
    )
    many = await statements()

    report = await _service.billable_report(db_session, w.project, w.admin, *SPAN, policy=POLICY)
    assert len(report.entries) == 61
    assert report.total.unconverted == ()
    assert many == few
    assert many <= 8


# ---------------------------------------------------------------------------
# Rates as entered
# ---------------------------------------------------------------------------


async def test_create_stores_the_rate_in_its_currency(db_session: AsyncSession, world: World) -> None:
    w = world
    rate = await _service.create_rate(
        db_session,
        w.project.id,
        BillingRateCreate(
            user_id=w.alice.id, hourly_rate=MoneyIn(amount="1.250", currency="BHD"), effective_from=SPAN[0]
        ),
    )
    assert (rate.hourly_rate, rate.currency_code) == (Decimal("1.25"), "BHD")
    assert rate.rate == Money("1.25", "BHD")
    out = BillingRateOut.from_model(rate)
    assert (Decimal(out.hourly_rate.amount), out.hourly_rate.currency) == (Decimal("1.25"), "BHD")


@pytest.mark.parametrize(
    ("amount", "currency"),
    [("1500.5", "JPY"), ("10.505", "USD"), ("1.2345", "BHD"), ("0", "USD"), ("-5", "THB")],
)
async def test_rates_beyond_the_minor_units_or_not_positive_are_refused(amount: str, currency: str) -> None:
    with pytest.raises(PydanticValidationError):
        BillingRateCreate(hourly_rate=MoneyIn(amount=amount, currency=currency), effective_from=SPAN[0])
    with pytest.raises(PydanticValidationError):
        BillingRateUpdate(hourly_rate=MoneyIn(amount=amount, currency=currency))


async def test_a_disabled_currency_cannot_be_chosen_but_an_existing_rate_keeps_it(
    db_session: AsyncSession, world: World
) -> None:
    w = world
    (rate,) = await _add(db_session, _rate(w, "1000", "THB", SPAN[0]))
    await db_session.execute(update(Currency).where(Currency.code == "THB").values(is_active=False))
    await db_session.flush()

    with pytest.raises(AppError) as refused:
        await _service.create_rate(
            db_session,
            w.project.id,
            BillingRateCreate(hourly_rate=MoneyIn(amount="1000", currency="THB"), effective_from=SPAN[0]),
        )
    assert refused.value.code == "currency_inactive"

    updated = await _service.update_rate(
        db_session, rate.id, w.project.id, BillingRateUpdate(hourly_rate=MoneyIn(amount="1200", currency="THB"))
    )
    assert updated.rate == Money("1200", "THB")

    changed = await _service.update_rate(
        db_session, rate.id, w.project.id, BillingRateUpdate(hourly_rate=MoneyIn(amount="35.50", currency="EUR"))
    )
    assert changed.rate == Money("35.5", "EUR")
