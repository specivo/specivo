"""Billing rate service: rate CRUD, rate resolution and the billable report."""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import NotFoundError, ValidationError
from specivo.core.exchange import ConversionSummary, RatePolicy
from specivo.core.money import Money, MoneyError, check_minor_units
from specivo.models.billing_rate import BillingRate
from specivo.models.project import Project
from specivo.models.time_entry import TimeEntry
from specivo.models.user import User
from specivo.schemas.billing_rate import BillingRateCreate, BillingRateUpdate
from specivo.schemas.money import MoneyIn
from specivo.services.currency_service import CurrencyService, reporting_currency_for
from specivo.services.exchange_rate_service import ExchangeRateService

logger = logging.getLogger(__name__)

_currencies = CurrencyService()
_exchange = ExchangeRateService()


# ---------------------------------------------------------------------------
# Report contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BillableEntry:
    """A billable time entry with the rate that applied on its date."""

    time_entry_id: int
    user_id: int
    spent_on: date
    hours: Decimal
    rate_id: int
    rate: Money
    amount: Money


@dataclass(frozen=True, slots=True)
class UnratedEntry:
    """A billable time entry with no effective rate on its date."""

    time_entry_id: int
    user_id: int
    spent_on: date
    hours: Decimal


@dataclass(frozen=True, slots=True)
class CurrencySubtotal:
    """The exact total of the rated entries in one rate currency."""

    currency: str
    hours: Decimal
    entry_count: int
    amount: Money


@dataclass(frozen=True, slots=True)
class BillableReport:
    """Billable time in a period.

    ``subtotals`` are exact, one per rate currency. ``total`` converts each
    entry's amount on its own date into the reporting currency; amounts with
    no usable exchange rate are in ``total.unconverted``, never dropped.
    Entries without an effective billing rate are in ``unrated``.
    """

    date_from: date
    date_to: date
    entries: tuple[BillableEntry, ...]
    unrated: tuple[UnratedEntry, ...]
    subtotals: tuple[CurrencySubtotal, ...]
    total: ConversionSummary

    @property
    def currency(self) -> str:
        return self.total.currency

    @property
    def rated_hours(self) -> Decimal:
        return sum((e.hours for e in self.entries), Decimal("0"))

    @property
    def unrated_hours(self) -> Decimal:
        return sum((u.hours for u in self.unrated), Decimal("0"))


# ---------------------------------------------------------------------------
# Rate resolution (in memory)
# ---------------------------------------------------------------------------


class RateTable:
    """A project's rates, resolved per user and date without further queries.

    A rate for the user wins over the project's rate (``user_id`` NULL); within
    either, the newest ``effective_from`` on or before the date wins, and the
    higher id breaks a tie on the same day.
    """

    def __init__(self, rates: Iterable[BillingRate]) -> None:
        by_user: dict[int | None, list[BillingRate]] = defaultdict(list)
        for rate in rates:
            by_user[rate.user_id].append(rate)
        for items in by_user.values():
            items.sort(key=lambda r: (r.effective_from, r.id), reverse=True)
        self._by_user = dict(by_user)

    def _newest(self, user_id: int | None, on: date) -> BillingRate | None:
        for rate in self._by_user.get(user_id, ()):
            if rate.effective_from <= on:
                return rate
        return None

    def effective(self, user_id: int, on: date) -> BillingRate | None:
        return self._newest(user_id, on) or self._newest(None, on)


class BillingRateService:
    """Service layer for billing rate management and reporting."""

    # -------------------------------------------------------------------
    # CRUD
    # -------------------------------------------------------------------

    async def create_rate(
        self,
        session: AsyncSession,
        project_id: int,
        data: BillingRateCreate,
    ) -> BillingRate:
        amount, currency = await self._checked_rate(session, data.hourly_rate, current_currency=None)
        rate = BillingRate(
            user_id=data.user_id,
            project_id=project_id,
            hourly_rate=amount,
            currency_code=currency,
            effective_from=data.effective_from,
        )
        session.add(rate)
        await session.flush()
        await session.refresh(rate)
        return rate

    async def list_rates(self, session: AsyncSession, project_id: int) -> list[BillingRate]:
        result = await session.execute(
            select(BillingRate).where(BillingRate.project_id == project_id).order_by(BillingRate.effective_from.desc())
        )
        return list(result.scalars().all())

    async def update_rate(
        self,
        session: AsyncSession,
        rate_id: int,
        project_id: int,
        data: BillingRateUpdate,
    ) -> BillingRate:
        rate = await self._get(session, rate_id, project_id)

        if data.hourly_rate is not None:
            amount, currency = await self._checked_rate(session, data.hourly_rate, current_currency=rate.currency_code)
            rate.hourly_rate = amount
            rate.currency_code = currency
        if data.effective_from is not None:
            rate.effective_from = data.effective_from

        await session.flush()
        await session.refresh(rate)
        return rate

    async def delete_rate(self, session: AsyncSession, rate_id: int, project_id: int) -> None:
        rate = await self._get(session, rate_id, project_id)
        await session.delete(rate)
        await session.flush()

    async def _get(self, session: AsyncSession, rate_id: int, project_id: int) -> BillingRate:
        result = await session.execute(
            select(BillingRate).where(BillingRate.id == rate_id).where(BillingRate.project_id == project_id)
        )
        rate = result.scalar_one_or_none()
        if rate is None:
            raise NotFoundError(f"Billing rate {rate_id} not found")
        return rate

    async def _checked_rate(
        self, session: AsyncSession, value: MoneyIn, *, current_currency: str | None
    ) -> tuple[Decimal, str]:
        """The amount and currency of an entered rate.

        The amount must be positive and within the currency's ISO minor units.
        A newly chosen currency must be enabled; keeping the rate's current
        currency is allowed even if it was disabled since.
        """
        money = value.to_money()
        if money.currency != current_currency:
            await _currencies.require_selectable(session, money.currency, field="hourly_rate.currency")
        if money.amount <= 0:
            raise ValidationError("hourly_rate must be greater than zero", field="hourly_rate.amount")
        try:
            check_minor_units(money.amount, money.currency)
        except MoneyError as exc:
            raise ValidationError(str(exc), field="hourly_rate.amount") from None
        return money.amount, money.currency

    # -------------------------------------------------------------------
    # Rate resolution
    # -------------------------------------------------------------------

    async def load_rate_table(
        self,
        session: AsyncSession,
        project_id: int,
        user_ids: Iterable[int],
        until: date,
    ) -> RateTable:
        """The project's rates for *user_ids* and the project-wide ones, effective by *until*, in one query."""
        users = sorted(set(user_ids))
        stmt = (
            select(BillingRate)
            .where(BillingRate.project_id == project_id)
            .where(BillingRate.effective_from <= until)
            .where(or_(BillingRate.user_id.is_(None), BillingRate.user_id.in_(users)))
        )
        return RateTable((await session.execute(stmt)).scalars().all())

    async def get_effective_rate(
        self,
        session: AsyncSession,
        user_id: int,
        project_id: int,
        for_date: date,
    ) -> BillingRate | None:
        """The rate for *user_id* on *for_date*: the user's own rate, else the project's."""
        table = await self.load_rate_table(session, project_id, [user_id], for_date)
        return table.effective(user_id, for_date)

    # -------------------------------------------------------------------
    # Billing report
    # -------------------------------------------------------------------

    async def billable_report(
        self,
        session: AsyncSession,
        project: Project,
        viewer: User,
        date_from: date,
        date_to: date,
        *,
        currency: str | None = None,
        policy: RatePolicy | None = None,
    ) -> BillableReport:
        """Billable time entries times their effective rate, per currency and converted.

        Only the entries *viewer* may read count (``TimeEntryService``), so a
        role restricted to its own time reports on its own time. *currency*
        defaults to the project's reporting currency; *policy* to the
        instance's exchange rate policy (``entry_date``). The query count does
        not depend on the number of entries: the rates are loaded in one query
        and the conversion in at most two.
        """
        from specivo.services.time_entry_service import TimeEntryService

        stmt = (
            select(TimeEntry)
            .where(TimeEntry.project_id == project.id)
            .where(TimeEntry.is_billable.is_(True))
            .where(TimeEntry.spent_on >= date_from)
            .where(TimeEntry.spent_on <= date_to)
            .order_by(TimeEntry.spent_on, TimeEntry.id)
        )
        visible = await TimeEntryService().visible_entries_clause(session, viewer, project)
        if visible is not None:
            stmt = stmt.where(visible)
        time_entries = list((await session.execute(stmt)).scalars().all())

        rates = (
            await self.load_rate_table(session, project.id, (e.user_id for e in time_entries), date_to)
            if time_entries
            else RateTable(())
        )
        entries: list[BillableEntry] = []
        unrated: list[UnratedEntry] = []
        for entry in time_entries:
            rate = rates.effective(entry.user_id, entry.spent_on)
            if rate is None:
                unrated.append(UnratedEntry(entry.id, entry.user_id, entry.spent_on, entry.hours))
                continue
            hourly = rate.rate
            entries.append(
                BillableEntry(
                    time_entry_id=entry.id,
                    user_id=entry.user_id,
                    spent_on=entry.spent_on,
                    hours=entry.hours,
                    rate_id=rate.id,
                    rate=hourly,
                    amount=hourly * entry.hours,
                )
            )

        target = currency or await reporting_currency_for(session, project)
        total = await _exchange.summarize(session, [(e.amount, e.spent_on) for e in entries], target, policy)
        return BillableReport(
            date_from=date_from,
            date_to=date_to,
            entries=tuple(entries),
            unrated=tuple(unrated),
            subtotals=_subtotals(entries),
            total=total,
        )


def _subtotals(entries: Sequence[BillableEntry]) -> tuple[CurrencySubtotal, ...]:
    groups: dict[str, list[BillableEntry]] = defaultdict(list)
    for entry in entries:
        groups[entry.amount.currency].append(entry)
    return tuple(
        CurrencySubtotal(
            currency=code,
            hours=sum((e.hours for e in items), Decimal("0")),
            entry_count=len(items),
            amount=sum((e.amount for e in items), Money.zero(code)),
        )
        for code, items in sorted(groups.items())
    )
