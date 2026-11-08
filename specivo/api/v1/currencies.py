"""Currencies API: the read-only list for pickers, and the admin list and switch."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from specivo.api.v1.admin import require_admin_api
from specivo.core.database import DbSession
from specivo.core.i18n import get_locale
from specivo.core.money import currency_display_name, currency_display_symbol
from specivo.core.security import get_current_user
from specivo.models.currency import Currency
from specivo.models.user import User
from specivo.schemas.money import CurrencyAdminOut, CurrencyOut, CurrencyUpdate
from specivo.services.currency_service import CurrencyService

router = APIRouter(tags=["currencies"])
_service = CurrencyService()


def currency_out(currency: Currency, locale: str) -> CurrencyOut:
    return CurrencyOut(
        code=currency.code,
        name=currency_display_name(currency.code, locale, fallback=currency.name),
        symbol=currency_display_symbol(currency.code, locale),
        minor_units=currency.minor_units,
    )


def currency_admin_out(currency: Currency, locale: str) -> CurrencyAdminOut:
    return CurrencyAdminOut(
        **currency_out(currency, locale).model_dump(),
        iso_name=currency.name,
        numeric_code=currency.numeric_code,
        kind=currency.kind,
        is_active=currency.is_active,
    )


@router.get("/currencies/", response_model=list[CurrencyOut])
async def list_currencies(
    db: DbSession,
    current_user: User = Depends(get_current_user),
    include: str | None = Query(None, description="Also return this code when it is disabled (a value already held)"),
) -> list[CurrencyOut]:
    """The currencies enabled on this instance, for pickers, ordered by code.

    Names and symbols are in the reader's language. Any signed-in user may read
    the list.
    """
    locale = get_locale()
    return [currency_out(c, locale) for c in await _service.list_currencies(db, active_only=True, include=include)]


@router.get("/admin/currencies/", response_model=list[CurrencyAdminOut])
async def admin_list_currencies(
    db: DbSession,
    current_user: User = Depends(require_admin_api),
    active: bool | None = Query(None, description="Only enabled (true) or disabled (false) currencies"),
) -> list[CurrencyAdminOut]:
    """Every ISO 4217 currency with its kind and whether it is enabled (admin only)."""
    locale = get_locale()
    currencies = await _service.list_currencies(db)
    if active is not None:
        currencies = [c for c in currencies if c.is_active is active]
    return [currency_admin_out(c, locale) for c in currencies]


@router.patch("/admin/currencies/{code}/", response_model=CurrencyAdminOut)
async def admin_update_currency(
    code: str,
    data: CurrencyUpdate,
    request: Request,
    db: DbSession,
    current_user: User = Depends(require_admin_api),
) -> CurrencyAdminOut:
    """Enable or disable a currency in pickers (admin only).

    Disabling hides the currency from pickers only: amounts, prices and
    reporting currencies that already use it keep working. Audited.
    """
    currency = await _service.set_active(db, code, data.is_active, current_user, request=request)
    return currency_admin_out(currency, get_locale())
