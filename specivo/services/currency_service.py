"""Currencies and the reporting currency.

- The ``currencies`` table (ISO 4217, seeded from ``specivo/data/iso4217.csv``):
  listing, switching a currency on or off for pickers, and re-syncing it from
  the CSV after a refresh.
- The reporting currency: the instance setting ``reporting_currency`` (USD by
  default) and the optional per-project override
  ``projects.reporting_currency_code``. :func:`reporting_currency_for` resolves
  which one applies.

Every change made here is written to the security audit log
(``currency_toggled``, ``reporting_currency_changed``).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.currency_data import CURRENCY_CODE_RE, DEFAULT_REPORTING_CURRENCY, load_iso4217
from specivo.core.exceptions import AppError, NotFoundError
from specivo.models.currency import Currency
from specivo.models.project import Project
from specivo.models.setting import Setting
from specivo.services.security_audit_service import AuditEvent, SecurityAuditService

if TYPE_CHECKING:
    from fastapi import Request

    from specivo.models.user import User

logger = logging.getLogger(__name__)

REPORTING_CURRENCY_SETTING_KEY = "reporting_currency"

_audit = SecurityAuditService()


def _normalize(code: str) -> str:
    return (code or "").strip().upper()


class CurrencyService:
    """Reads and changes currencies and reporting currencies."""

    # -- currencies -----------------------------------------------------------

    async def list_currencies(
        self,
        session: AsyncSession,
        *,
        active_only: bool = False,
        include: str | None = None,
    ) -> list[Currency]:
        """Currencies ordered by code; *active_only* for pickers.

        *include* adds that code even when it is inactive, so a picker can
        still show the value a record already holds.
        """
        stmt = select(Currency).order_by(Currency.code)
        if active_only:
            if include:
                stmt = stmt.where((Currency.is_active.is_(True)) | (Currency.code == _normalize(include)))
            else:
                stmt = stmt.where(Currency.is_active.is_(True))
        return list((await session.execute(stmt)).scalars().all())

    async def get(self, session: AsyncSession, code: str) -> Currency:
        """The currency *code* (any case); 404 when it does not exist."""
        normalized = _normalize(code)
        currency = await session.get(Currency, normalized) if CURRENCY_CODE_RE.match(normalized) else None
        if currency is None:
            raise NotFoundError(f"Currency '{code}' not found")
        return currency

    async def require_selectable(self, session: AsyncSession, code: str, *, field: str = "currency") -> Currency:
        """The currency *code* if it exists and is active, else a 422 on *field*.

        For values someone chooses now. A stored value that refers to a
        currency deactivated since stays valid and is not re-checked.
        """
        normalized = _normalize(code)
        currency = await session.get(Currency, normalized) if CURRENCY_CODE_RE.match(normalized) else None
        if currency is None:
            raise AppError(
                code="currency_unknown",
                message=f"Unknown currency '{code}'",
                status_code=422,
                field=field,
            )
        if not currency.is_active:
            raise AppError(
                code="currency_inactive",
                message=f"Currency '{currency.code}' is not enabled on this instance",
                status_code=422,
                field=field,
            )
        return currency

    async def set_active(
        self,
        session: AsyncSession,
        code: str,
        is_active: bool,
        actor: User,
        request: Request | None = None,
    ) -> Currency:
        """Enable or disable *code* in pickers; audited when it changes.

        Disabling never touches what already references the currency.
        """
        currency = await self.get(session, code)
        if currency.is_active == is_active:
            return currency
        currency.is_active = is_active
        await session.flush()
        await self._audit(
            session,
            AuditEvent.CURRENCY_TOGGLED,
            actor,
            request,
            {"code": currency.code, "is_active": is_active},
        )
        return currency

    async def sync_from_csv(self, session: AsyncSession) -> dict[str, int]:
        """Bring the table in line with ``specivo/data/iso4217.csv``.

        New codes are inserted with the CSV's ``is_active``; existing codes get
        the CSV's name, numeric code, minor units, symbol and kind but keep
        their ``is_active`` (an administrator's choice). Codes that left the
        list are deactivated, never deleted. Returns counts per outcome.
        """
        rows = load_iso4217()
        existing = {c.code: c for c in (await session.execute(select(Currency))).scalars().all()}
        counts = {"inserted": 0, "updated": 0, "deactivated": 0}
        listed = set()
        for row in rows:
            listed.add(row.code)
            values = {
                "numeric_code": row.numeric_code,
                "name": row.name,
                "minor_units": row.minor_units,
                "symbol": row.symbol,
                "kind": row.kind,
            }
            current = existing.get(row.code)
            if current is None:
                counts["inserted"] += 1
            elif any(getattr(current, k) != v for k, v in values.items()):
                counts["updated"] += 1
            else:
                continue
            stmt = pg_insert(Currency).values(code=row.code, is_active=row.is_active, **values)
            stmt = stmt.on_conflict_do_update(index_elements=["code"], set_=values)
            await session.execute(stmt)
        for code, currency in existing.items():
            if code not in listed and currency.is_active:
                currency.is_active = False
                counts["deactivated"] += 1
        await session.flush()
        session.expire_all()
        return counts

    # -- reporting currency ---------------------------------------------------

    async def get_instance_reporting_currency(self, session: AsyncSession) -> str:
        """The instance reporting currency; USD when the setting is missing or malformed."""
        value = await session.scalar(select(Setting.value).where(Setting.key == REPORTING_CURRENCY_SETTING_KEY))
        normalized = _normalize(value or "")
        return normalized if CURRENCY_CODE_RE.match(normalized) else DEFAULT_REPORTING_CURRENCY

    async def set_instance_reporting_currency(
        self,
        session: AsyncSession,
        code: str,
        actor: User,
        request: Request | None = None,
    ) -> str:
        """Set the instance reporting currency to an active currency; audited when it changes."""
        old = await self.get_instance_reporting_currency(session)
        currency = await self.require_selectable(session, code, field="code") if _normalize(code) != old else None
        new = currency.code if currency else old
        stmt = pg_insert(Setting).values(key=REPORTING_CURRENCY_SETTING_KEY, value=new)
        stmt = stmt.on_conflict_do_update(index_elements=["key"], set_={"value": new})
        await session.execute(stmt)
        await session.flush()
        if new != old:
            await self._audit(
                session,
                AuditEvent.REPORTING_CURRENCY_CHANGED,
                actor,
                request,
                {"scope": "instance", "old": old, "new": new},
            )
        return new

    async def reporting_currency_for(self, session: AsyncSession, project: Project | None) -> str:
        """The reporting currency for *project*: its override, else the instance setting."""
        if project is not None and project.reporting_currency_code:
            return project.reporting_currency_code
        return await self.get_instance_reporting_currency(session)

    async def set_project_reporting_currency(
        self,
        session: AsyncSession,
        project: Project,
        code: str | None,
        actor: User | None,
        request: Request | None = None,
    ) -> None:
        """Set or clear (``None`` / ``""``: inherit) *project*'s override; audited when it changes.

        A new value must be an active currency; keeping the current value
        is always allowed, even if that currency was disabled since.
        """
        old = project.reporting_currency_code
        normalized = _normalize(code or "") or None
        if normalized == old:
            return
        if normalized is not None:
            normalized = (await self.require_selectable(session, normalized, field="reporting_currency_code")).code
        project.reporting_currency_code = normalized
        await session.flush()
        await self._audit(
            session,
            AuditEvent.REPORTING_CURRENCY_CHANGED,
            actor,
            request,
            {"scope": "project", "project_key": project.key, "old": old, "new": normalized},
            project_id=project.id,
        )

    # -- helpers --------------------------------------------------------------

    async def _audit(
        self,
        session: AsyncSession,
        event: AuditEvent,
        actor: User | None,
        request: Request | None,
        details: dict[str, Any],
        project_id: int | None = None,
    ) -> None:
        # An audit write never breaks the change it records.
        try:
            await _audit.log_currency_change(
                session,
                event,
                user_id=actor.id if actor is not None else None,
                details=details,
                project_id=project_id,
                request=request,
            )
        except Exception:
            logger.exception("Failed to write the %s audit event", event)


async def reporting_currency_for(session: AsyncSession, project: Project | None) -> str:
    """The reporting currency for *project* (``None``: the instance's)."""
    return await CurrencyService().reporting_currency_for(session, project)
