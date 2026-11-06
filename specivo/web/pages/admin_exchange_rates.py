"""Web admin page for exchange rates: list, manual entry, CSV import, settings, fetch and a conversion preview.

Plain server-rendered forms (no script). Writes redirect back to the page
with a ``notice`` or ``error`` code; the CSV import and the conversion preview
render their result in place.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.database import DB_SESSION
from specivo.core.exceptions import AppError, NotFoundError
from specivo.core.exchange import Converted
from specivo.core.i18n import get_locale
from specivo.core.money import (
    Money,
    MoneyError,
    check_minor_units,
    currency_display_name,
    format_decimal_plain,
    to_decimal,
)
from specivo.core.utils import utcnow
from specivo.models.user import User
from specivo.services.currency_service import CurrencyService
from specivo.services.exchange_rate_feed import DEFAULT_FEED_URL
from specivo.services.exchange_rate_service import (
    CSV_MAX_BYTES,
    CsvImportResult,
    ExchangeRateService,
    parse_policy,
)
from specivo.web.deps import get_templates
from specivo.web.pages.admin import require_admin

router = APIRouter(tags=["web-admin"], include_in_schema=False)
_service = ExchangeRateService()

_BASE_URL = "/admin/exchange-rates/"
_SOURCES = ("manual", "csv", "ecb")
_NOTICES = frozenset({"created", "updated", "deleted", "settings_saved", "fetched", "fetch_failed"})
_ERRORS = frozenset(
    {
        "currency_unknown",
        "currency_inactive",
        "exchange_rate_invalid",
        "exchange_rate_same_currency",
        "exchange_rate_exists",
        "exchange_rate_not_editable",
        "exchange_rate_max_age_invalid",
        "exchange_rate_fetch_url_invalid",
        "date_invalid",
        "not_found",
    }
)


def _parse_day(value: str) -> date | None:
    try:
        return date.fromisoformat(value.strip()) if value and value.strip() else None
    except ValueError:
        return None


def _redirect(params: dict[str, str] | None = None, *, anchor: str = "") -> RedirectResponse:
    query = urlencode({k: v for k, v in (params or {}).items() if v})
    return RedirectResponse(_BASE_URL + (f"?{query}" if query else "") + (f"#{anchor}" if anchor else ""), 303)


async def _currency_choices(db: AsyncSession, locale: str) -> list[dict[str, str]]:
    return [
        {"code": c.code, "name": currency_display_name(c.code, locale, fallback=c.name)}
        for c in await CurrencyService().list_currencies(db, active_only=True)
    ]


async def _render(
    request: Request,
    user: User,
    db: AsyncSession,
    *,
    filters: dict[str, str],
    offset: int,
    limit: int,
    notice: str = "",
    error: str = "",
    import_result: CsvImportResult | None = None,
    import_text: str = "",
    preview: dict[str, Any] | None = None,
    status_code: int = 200,
) -> Response:
    locale = get_locale()
    settings = await _service.get_settings(db)
    rows, total = await _service.list_rates(
        db,
        currency=filters.get("currency") or None,
        source=filters.get("source") or None,
        date_from=_parse_day(filters.get("date_from", "")),
        date_to=_parse_day(filters.get("date_to", "")),
        offset=offset,
        limit=limit,
    )
    return get_templates().TemplateResponse(
        request,
        "pages/admin/exchange_rates.html",
        status_code=status_code,
        context={
            "user": user,
            "active_page": "admin",
            "rates": [
                {
                    "id": r.id,
                    "rate_date": r.rate_date,
                    "base": r.base_code,
                    "quote": r.quote_code,
                    "rate": rate_text(r.rate),
                    "source": r.source,
                }
                for r in rows
            ],
            "locale": locale,
            "total": total,
            "offset": offset,
            "limit": limit,
            "filters": filters,
            "page_query": urlencode({k: v for k, v in filters.items() if v}),
            "sources": _SOURCES,
            "currencies": await _currency_choices(db, locale),
            "settings": settings,
            "default_feed_url": DEFAULT_FEED_URL,
            "today": utcnow().date().isoformat(),
            "notice": notice if notice in _NOTICES else "",
            "error": error if error in _ERRORS else "",
            "import_result": import_result,
            "import_text": import_text,
            "csv_max_mb": CSV_MAX_BYTES // (1024 * 1024),
            "preview": preview,
        },
    )


@router.get("/admin/exchange-rates/", response_class=HTMLResponse)
async def admin_exchange_rates(
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
    currency: str = Query(""),
    source: str = Query(""),
    date_from: str = Query(""),
    date_to: str = Query(""),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    notice: str = Query(""),
    error: str = Query(""),
    # Conversion preview
    amount: str = Query(""),
    from_currency: str = Query("", alias="from"),
    to: str = Query(""),
    on: str = Query(""),
    policy: str = Query(""),
) -> Response:
    """The exchange rates page; ``?amount=&from=&to=&on=&policy=`` also previews a conversion."""
    filters = {
        "currency": currency.strip().upper()[:3],
        "source": source if source in _SOURCES else "",
        "date_from": date_from if _parse_day(date_from) else "",
        "date_to": date_to if _parse_day(date_to) else "",
    }
    preview = None
    if amount or from_currency or to:
        preview = await _preview(db, amount, from_currency, to, on, policy)
    return await _render(
        request, user, db, filters=filters, offset=offset, limit=limit, notice=notice, error=error, preview=preview
    )


async def _preview(
    db: AsyncSession, amount: str, from_currency: str, to: str, on: str, policy_text: str
) -> dict[str, Any]:
    """Run the conversion preview; returns the form values and either a result or an error message key."""
    form = {
        "amount": amount.strip(),
        "from": from_currency.strip().upper(),
        "to": to.strip().upper(),
        "on": on,
        "policy": policy_text,
    }
    day = _parse_day(on) or utcnow().date()
    form["on"] = day.isoformat()
    try:
        value = to_decimal(form["amount"])
        money = Money(value, form["from"])
        check_minor_units(value, money.currency)
        Money(0, form["to"])
    except (MoneyError, TypeError) as exc:
        return {"form": form, "error": str(exc)}
    settings = await _service.get_settings(db)
    try:
        rate_policy = parse_policy(policy_text, settings.max_age_days)
    except AppError as exc:
        return {"form": form, "error": exc.message}
    result = await _service.convert(db, money, form["to"], day, rate_policy)
    preview: dict[str, Any] = {"form": form, "result": result, "policy": str(rate_policy)}
    if isinstance(result, Converted):
        preview["rate_text"] = rate_text(result.rate.rate, places=10)
    return preview


def rate_text(rate: Decimal, places: int = 12) -> str:
    """A rate for display: at most *places* decimals (half up), trailing zeros dropped (``36.5``)."""
    rounded = rate.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP).normalize()
    return format_decimal_plain(rounded)


@router.post("/admin/exchange-rates/", response_model=None)
async def admin_exchange_rate_create(
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
    rate_date: str = Form(""),
    base: str = Form(""),
    quote: str = Form(""),
    rate: str = Form(""),
) -> Response:
    """Add a manual rate, then return to the page with a notice or the error code."""
    day = _parse_day(rate_date)
    if day is None:
        return _redirect({"error": "date_invalid"}, anchor="add-rate")
    try:
        await _service.create_manual(db, rate_date=day, base=base, quote=quote, rate=rate, actor=user, request=request)
    except AppError as exc:
        return _redirect({"error": exc.code}, anchor="add-rate")
    return _redirect({"notice": "created"})


@router.get("/admin/exchange-rates/{rate_id}/edit/", response_class=HTMLResponse)
async def admin_exchange_rate_edit(
    rate_id: int,
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
    error: str = Query(""),
) -> Response:
    """The form for editing a manual rate."""
    try:
        row = await _service.get_rate(db, rate_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Exchange rate not found") from None
    if row.source != "manual":
        return _redirect({"error": "exchange_rate_not_editable"})
    choices = await _currency_choices(db, get_locale())
    codes = {c["code"] for c in choices}
    for code in (row.base_code, row.quote_code):
        if code not in codes:
            choices.append({"code": code, "name": code})
    return get_templates().TemplateResponse(
        request,
        "pages/admin/exchange_rate_edit.html",
        context={
            "user": user,
            "active_page": "admin",
            "rate": row,
            "rate_value": rate_text(row.rate),
            "currencies": sorted(choices, key=lambda c: c["code"]),
            "error": error if error in _ERRORS else "",
        },
    )


@router.post("/admin/exchange-rates/{rate_id}/edit/", response_model=None)
async def admin_exchange_rate_update(
    rate_id: int,
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
    rate_date: str = Form(""),
    base: str = Form(""),
    quote: str = Form(""),
    rate: str = Form(""),
) -> Response:
    """Save an edited manual rate."""
    day = _parse_day(rate_date)
    edit_url = f"{_BASE_URL}{rate_id}/edit/"
    if day is None:
        return RedirectResponse(f"{edit_url}?error=date_invalid", 303)
    try:
        await _service.update_manual(
            db, rate_id, actor=user, request=request, rate_date=day, base=base, quote=quote, rate=rate
        )
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Exchange rate not found") from None
    except AppError as exc:
        return RedirectResponse(f"{edit_url}?error={exc.code}", 303)
    return _redirect({"notice": "updated"})


@router.post("/admin/exchange-rates/{rate_id}/delete/", response_model=None)
async def admin_exchange_rate_delete(
    rate_id: int,
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
) -> Response:
    """Delete a rate of any source."""
    try:
        await _service.delete_rate(db, rate_id, actor=user, request=request)
    except NotFoundError:
        return _redirect({"error": "not_found"})
    return _redirect({"notice": "deleted"})


@router.post("/admin/exchange-rates/import/", response_class=HTMLResponse)
async def admin_exchange_rate_import(
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
    file: UploadFile | None = File(None),
    csv_text: str = Form(""),
    action: str = Form("preview"),
) -> Response:
    """Preview (dry run) or apply a CSV import; the page shows the outcome and every bad line."""
    content: bytes | str = csv_text
    if file is not None and file.filename:
        content = await file.read(CSV_MAX_BYTES + 1)
    dry_run = action != "import"
    result = await _service.import_csv(db, content, actor=user, request=request, dry_run=dry_run)
    text = content if isinstance(content, str) else ""
    return await _render(
        request,
        user,
        db,
        filters={},
        offset=0,
        limit=50,
        import_result=result,
        import_text=text if len(text) <= 200_000 else "",
        status_code=422 if result.errors and not dry_run else 200,
    )


@router.post("/admin/exchange-rates/settings/", response_model=None)
async def admin_exchange_rate_settings(
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
    pivot: str = Form(""),
    max_age_days: str = Form(""),
    fetch_enabled: str = Form(""),
    fetch_url: str = Form(""),
) -> Response:
    """Save the pivot, the maximum age and the fetch switch and URL."""
    try:
        max_age = int(max_age_days.strip()) if max_age_days.strip() else None
    except ValueError:
        return _redirect({"error": "exchange_rate_max_age_invalid"}, anchor="rate-settings")
    try:
        await _service.update_settings(
            db,
            user,
            request,
            pivot=pivot or None,
            max_age_days=max_age,
            fetch_enabled=fetch_enabled == "1",
            fetch_url=fetch_url,
        )
    except AppError as exc:
        return _redirect({"error": exc.code}, anchor="rate-settings")
    return _redirect({"notice": "settings_saved"}, anchor="rate-settings")


@router.post("/admin/exchange-rates/fetch/", response_model=None)
async def admin_exchange_rate_fetch(
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
) -> Response:
    """Fetch the configured feed now (also when the daily fetch is off)."""
    result = await _service.fetch_and_store(db, force=True)
    return _redirect({"notice": "fetched" if result.ok else "fetch_failed"}, anchor="rate-settings")
