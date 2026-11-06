"""Exchange rates API (administrators): rates, CSV import, settings, the optional fetch and a conversion preview."""

from __future__ import annotations

from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request, Response

from specivo.api.v1.admin import require_admin_api
from specivo.core.database import DbSession
from specivo.models.user import User
from specivo.schemas.common import PaginatedResponse
from specivo.schemas.exchange_rate import (
    ConvertIn,
    ConvertOut,
    CsvLineErrorOut,
    CsvRowOut,
    ExchangeRateCreate,
    ExchangeRateImportIn,
    ExchangeRateImportOut,
    ExchangeRateOut,
    ExchangeRateSettingsOut,
    ExchangeRateSettingsUpdate,
    ExchangeRateUpdate,
    FetchResultOut,
)
from specivo.services.exchange_rate_service import (
    CsvImportResult,
    ExchangeRateService,
    ExchangeRateSettings,
    parse_policy,
)

router = APIRouter(prefix="/admin/exchange-rates", tags=["exchange-rates"])
_service = ExchangeRateService()


def settings_out(settings: ExchangeRateSettings) -> ExchangeRateSettingsOut:
    return ExchangeRateSettingsOut(
        pivot=settings.pivot,
        max_age_days=settings.max_age_days,
        fetch_enabled=settings.fetch_enabled,
        fetch_url=settings.fetch_url,
        last_fetch=settings.last_fetch,
    )


def import_out(result: CsvImportResult) -> ExchangeRateImportOut:
    return ExchangeRateImportOut(
        dry_run=result.dry_run,
        applied=result.applied,
        rows=result.rows,
        created=result.created,
        updated=result.updated,
        unchanged=result.unchanged,
        errors=[CsvLineErrorOut(line=e.line, code=e.code, message=e.message) for e in result.errors],
        preview=[
            CsvRowOut(
                line=r.line,
                rate_date=r.rate_date,
                base=r.base,
                quote=r.quote,
                rate=format(r.rate, "f"),
                action=r.action,  # type: ignore[arg-type]
            )
            for r in result.preview
        ],
    )


@router.get("/", response_model=PaginatedResponse[ExchangeRateOut])
async def list_rates(
    db: DbSession,
    current_user: User = Depends(require_admin_api),
    base: str | None = Query(None, min_length=3, max_length=3),
    quote: str | None = Query(None, min_length=3, max_length=3),
    currency: str | None = Query(None, min_length=3, max_length=3, description="Either side of the pair"),
    source: Literal["manual", "csv", "ecb"] | None = Query(None),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
) -> PaginatedResponse[ExchangeRateOut]:
    """Stored rates, newest day first (admin only)."""
    rows, total = await _service.list_rates(
        db,
        base=base,
        quote=quote,
        currency=currency,
        source=source,
        date_from=date_from,
        date_to=date_to,
        offset=offset,
        limit=limit,
    )
    return PaginatedResponse[ExchangeRateOut](
        total_count=total, offset=offset, limit=limit, items=[ExchangeRateOut.from_row(r) for r in rows]
    )


@router.post("/", response_model=ExchangeRateOut, status_code=201)
async def create_rate(
    data: ExchangeRateCreate,
    request: Request,
    db: DbSession,
    current_user: User = Depends(require_admin_api),
) -> ExchangeRateOut:
    """Add a manual rate: ``1 base = rate quote`` on ``rate_date`` (admin only).

    Both currencies must be enabled. A manual rate wins over an imported or
    fetched one of the same day. One manual rate per day and pair (409
    ``exchange_rate_exists``). Audited.
    """
    row = await _service.create_manual(
        db,
        rate_date=data.rate_date,
        base=data.base,
        quote=data.quote,
        rate=data.rate,
        actor=current_user,
        request=request,
    )
    await db.refresh(row)
    return ExchangeRateOut.from_row(row)


@router.get("/settings/", response_model=ExchangeRateSettingsOut)
async def get_settings(db: DbSession, current_user: User = Depends(require_admin_api)) -> ExchangeRateSettingsOut:
    """Pivot currency, maximum rate age and the optional fetch (admin only)."""
    return settings_out(await _service.get_settings(db))


@router.patch("/settings/", response_model=ExchangeRateSettingsOut)
async def update_settings(
    data: ExchangeRateSettingsUpdate,
    request: Request,
    db: DbSession,
    current_user: User = Depends(require_admin_api),
) -> ExchangeRateSettingsOut:
    """Change the pivot, the maximum age, the fetch switch or URL (admin only). Audited."""
    settings = await _service.update_settings(
        db,
        current_user,
        request,
        pivot=data.pivot,
        max_age_days=data.max_age_days,
        fetch_enabled=data.fetch_enabled,
        fetch_url=data.fetch_url,
    )
    return settings_out(settings)


@router.post("/import/", response_model=ExchangeRateImportOut)
async def import_rates(
    data: ExchangeRateImportIn,
    request: Request,
    response: Response,
    db: DbSession,
    current_user: User = Depends(require_admin_api),
) -> ExchangeRateImportOut:
    """Import ``date,base,quote,rate`` lines (admin only).

    All or nothing: with any bad line nothing is written and the answer is 422
    with every error and its line. ``dry_run`` reports what would happen,
    errors included, with 200 and without writing. Audited when applied.
    """
    result = await _service.import_csv(db, data.csv, actor=current_user, request=request, dry_run=data.dry_run)
    if result.errors and not data.dry_run:
        response.status_code = 422
    return import_out(result)


@router.post("/fetch/", response_model=FetchResultOut)
async def fetch_now(db: DbSession, current_user: User = Depends(require_admin_api)) -> FetchResultOut:
    """Fetch the configured feed now, even when the daily fetch is off (admin only).

    Useful to check the URL and the proxy. A failure is reported in the body
    (``ok: false``, ``error``), not as an HTTP error.
    """
    result = await _service.fetch_and_store(db, force=True)
    return FetchResultOut(
        ok=result.ok, stored=result.stored, skipped=result.skipped, rate_date=result.rate_date, error=result.error
    )


@router.post("/convert/", response_model=ConvertOut)
async def convert(data: ConvertIn, db: DbSession, current_user: User = Depends(require_admin_api)) -> ConvertOut:
    """Preview a conversion with the stored rates (admin only).

    The answer is either the converted amount with the rate and its date, or
    ``unconverted`` with the reason (``no_rate``, ``stale``).
    """
    settings = await _service.get_settings(db)
    policy = parse_policy(data.policy, settings.max_age_days)
    result = await _service.convert(db, data.money.to_money(), data.to, data.on, policy)
    return ConvertOut.from_result(result, str(policy))


@router.get("/{rate_id}/", response_model=ExchangeRateOut)
async def get_rate(rate_id: int, db: DbSession, current_user: User = Depends(require_admin_api)) -> ExchangeRateOut:
    return ExchangeRateOut.from_row(await _service.get_rate(db, rate_id))


@router.patch("/{rate_id}/", response_model=ExchangeRateOut)
async def update_rate(
    rate_id: int,
    data: ExchangeRateUpdate,
    request: Request,
    db: DbSession,
    current_user: User = Depends(require_admin_api),
) -> ExchangeRateOut:
    """Edit a manual rate (admin only). Imported and fetched rates are refused (422). Audited."""
    row = await _service.update_manual(
        db,
        rate_id,
        actor=current_user,
        request=request,
        rate_date=data.rate_date,
        base=data.base,
        quote=data.quote,
        rate=data.rate,
    )
    await db.refresh(row)
    return ExchangeRateOut.from_row(row)


@router.delete("/{rate_id}/", status_code=204)
async def delete_rate(
    rate_id: int, request: Request, db: DbSession, current_user: User = Depends(require_admin_api)
) -> Response:
    """Delete a rate of any source (admin only). Audited."""
    await _service.delete_rate(db, rate_id, actor=current_user, request=request)
    return Response(status_code=204)
