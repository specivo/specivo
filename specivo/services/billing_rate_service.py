"""Billing rate service — CRUD and billable report generation."""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import NotFoundError
from specivo.models.agent_cost import BillingRate
from specivo.models.time_entry import TimeEntry
from specivo.schemas.agent_cost import BillingRateCreate, BillingRateUpdate

logger = logging.getLogger(__name__)


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
        rate = BillingRate(
            user_id=data.user_id,
            project_id=project_id,
            hourly_rate=data.hourly_rate,
            currency=data.currency,
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
        result = await session.execute(
            select(BillingRate).where(BillingRate.id == rate_id).where(BillingRate.project_id == project_id)
        )
        rate = result.scalar_one_or_none()
        if rate is None:
            raise NotFoundError(f"Billing rate {rate_id} not found")

        if data.hourly_rate is not None:
            rate.hourly_rate = data.hourly_rate
        if data.currency is not None:
            rate.currency = data.currency
        if data.effective_from is not None:
            rate.effective_from = data.effective_from

        await session.flush()
        await session.refresh(rate)
        return rate

    async def delete_rate(self, session: AsyncSession, rate_id: int, project_id: int) -> None:
        result = await session.execute(
            select(BillingRate).where(BillingRate.id == rate_id).where(BillingRate.project_id == project_id)
        )
        rate = result.scalar_one_or_none()
        if rate is None:
            raise NotFoundError(f"Billing rate {rate_id} not found")
        await session.delete(rate)
        await session.flush()

    # -------------------------------------------------------------------
    # Rate resolution
    # -------------------------------------------------------------------

    async def get_effective_rate(
        self,
        session: AsyncSession,
        user_id: int,
        project_id: int,
        for_date: date,
    ) -> BillingRate | None:
        """Get the effective rate: user-specific > project-level.

        Returns the rate with effective_from <= for_date, preferring
        user-specific rates over project-level rates.
        """
        # Try user-specific rate first
        result = await session.execute(
            select(BillingRate)
            .where(BillingRate.project_id == project_id)
            .where(BillingRate.user_id == user_id)
            .where(BillingRate.effective_from <= for_date)
            .order_by(BillingRate.effective_from.desc())
            .limit(1)
        )
        rate = result.scalar_one_or_none()
        if rate is not None:
            return rate

        # Fall back to project-level rate (user_id IS NULL)
        result = await session.execute(
            select(BillingRate)
            .where(BillingRate.project_id == project_id)
            .where(BillingRate.user_id.is_(None))
            .where(BillingRate.effective_from <= for_date)
            .order_by(BillingRate.effective_from.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    # -------------------------------------------------------------------
    # Billing report
    # -------------------------------------------------------------------

    async def billable_report(
        self,
        session: AsyncSession,
        project_id: int,
        date_from: date,
        date_to: date,
    ) -> dict:
        """Generate a billing report: billable time entries * rate."""
        result = await session.execute(
            select(TimeEntry)
            .where(TimeEntry.project_id == project_id)
            .where(TimeEntry.is_billable.is_(True))
            .where(TimeEntry.spent_on >= date_from)
            .where(TimeEntry.spent_on <= date_to)
            .order_by(TimeEntry.spent_on)
        )
        entries = list(result.scalars().all())

        report_entries = []
        total_hours = Decimal("0")
        total_amount = Decimal("0")
        currency = "USD"

        for entry in entries:
            rate = await self.get_effective_rate(session, entry.user_id, project_id, entry.spent_on)
            if rate is None:
                continue

            amount = entry.hours * rate.hourly_rate
            currency = rate.currency
            total_hours += entry.hours
            total_amount += amount

            report_entries.append(
                {
                    "time_entry_id": entry.id,
                    "user_id": entry.user_id,
                    "hours": entry.hours,
                    "rate": rate.hourly_rate,
                    "amount": amount,
                    "spent_on": entry.spent_on,
                }
            )

        return {
            "total_hours": total_hours,
            "total_amount": total_amount,
            "currency": currency,
            "entries": report_entries,
        }
