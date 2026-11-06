"""Web admin pages for AI model prices: the price list, one model's history, new prices and the bundled seed.

Plain server-rendered forms (no script). Writes redirect back with a
``notice`` or ``error`` code. Instants are entered and shown in UTC.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.ai_pricing import normalize_model_name, normalize_provider
from specivo.core.ai_pricing_seed import PRICE_COLUMNS, SeedFormatError, format_price, load_snapshot
from specivo.core.database import DB_SESSION
from specivo.core.exceptions import AppError, NotFoundError
from specivo.core.i18n import get_locale
from specivo.core.money import currency_display_name
from specivo.core.utils import utcnow
from specivo.models.ai_model_price import AiModelPrice
from specivo.models.user import User
from specivo.services.ai_model_price_service import AiModelPriceService, PriceInput, price_usage_of
from specivo.services.currency_service import CurrencyService
from specivo.web.deps import get_templates
from specivo.web.pages.admin import require_admin

router = APIRouter(tags=["web-admin"], include_in_schema=False)
_service = AiModelPriceService()

_BASE_URL = "/admin/ai-model-prices/"
_MODEL_URL = "/admin/ai-model-prices/model/"
_SOURCES = ("seed", "admin")
_STATUSES = ("active", "inactive", "all")
_NOTICES = frozenset(
    {"created", "closed", "deactivated", "activated", "deleted", "ended", "saved", "seeded", "seed_unchanged"}
)
_ERRORS = frozenset(
    {
        "ai_model_price_invalid",
        "ai_model_price_overlap",
        "ai_model_price_in_use",
        "ai_model_price_not_deletable",
        "ai_model_price_immutable",
        "currency_unknown",
        "currency_inactive",
        "instant_invalid",
        "threshold_invalid",
        "seed_invalid",
        "not_found",
    }
)


def parse_utc(value: str) -> datetime | None:
    """A ``datetime-local`` value (``2026-11-06T09:30``) or a date, read as UTC; ``None`` if empty or invalid."""
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def utc_text(value: datetime | None) -> str:
    """An instant for display: ``2026-11-06 09:30 UTC`` (seconds shown only when set)."""
    if value is None:
        return ""
    v = value.astimezone(UTC)
    return v.strftime("%Y-%m-%d %H:%M:%S UTC" if v.second else "%Y-%m-%d %H:%M UTC")


def utc_input(value: datetime | None) -> str:
    """An instant as a ``datetime-local`` value in UTC."""
    if value is None:
        return ""
    v = value.astimezone(UTC)
    return v.strftime("%Y-%m-%dT%H:%M:%S" if v.second else "%Y-%m-%dT%H:%M")


def _redirect(url: str, params: dict[str, str] | None = None, *, anchor: str = "") -> RedirectResponse:
    query = urlencode({k: v for k, v in (params or {}).items() if v})
    return RedirectResponse(url + (f"?{query}" if query else "") + (f"#{anchor}" if anchor else ""), 303)


def _model_params(provider: str, model: str) -> dict[str, str]:
    return {"provider": provider, "model": model}


def price_row(row: AiModelPrice, now: datetime) -> dict[str, Any]:
    """A price row for the templates."""
    return {
        "id": row.id,
        "provider": row.provider,
        "model": row.model,
        "aliases": list(row.aliases or []),
        "currency": row.currency_code,
        "prices": {c: format_price(getattr(row, c)) for c in PRICE_COLUMNS},
        "long_context_threshold": row.long_context_threshold,
        "is_local": row.is_local,
        "baseline_model": row.baseline_model,
        "from": utc_text(row.effective_from),
        "to": utc_text(row.effective_to),
        "to_input": utc_input(row.effective_to),
        "source": row.source,
        "is_active": row.is_active,
        "is_current": row.is_active and row.contains(now),
        "note": row.note or "",
        "superseded_by_id": row.superseded_by_id,
        "history_query": urlencode(_model_params(row.provider, row.model)),
    }


async def _currency_choices(db: AsyncSession) -> list[dict[str, str]]:
    locale = get_locale()
    return [
        {"code": c.code, "name": currency_display_name(c.code, locale, fallback=c.name)}
        for c in await CurrencyService().list_currencies(db, active_only=True)
    ]


def _snapshot_info() -> dict[str, Any]:
    try:
        snapshot = load_snapshot()
    except (OSError, SeedFormatError, ValueError):
        return {"ok": False}
    return {
        "ok": True,
        "as_of": utc_text(snapshot.as_of),
        "count": len(snapshot.prices),
        "url": str(snapshot.source.get("url") or ""),
    }


@router.get("/admin/ai-model-prices/", response_class=HTMLResponse)
async def admin_ai_model_prices(
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
    provider: str = Query(""),
    q: str = Query(""),
    source: str = Query(""),
    status: str = Query("active"),
    current: str = Query("1"),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    notice: str = Query(""),
    error: str = Query(""),
    inserted: int = Query(0, ge=0),
    closed: int = Query(0, ge=0),
) -> Response:
    """The price list: by default the prices valid now, one line per model and source."""
    now = utcnow()
    filters = {
        "provider": normalize_provider(provider) or "",
        "q": q.strip()[:200],
        "source": source if source in _SOURCES else "",
        "status": status if status in _STATUSES else "active",
        "current": "1" if current == "1" else "0",
    }
    rows, total = await _service.list_prices(
        db,
        provider=filters["provider"] or None,
        q=filters["q"] or None,
        source=filters["source"] or None,
        status=filters["status"],
        valid_at=now if filters["current"] == "1" else None,
        offset=offset,
        limit=limit,
    )
    return get_templates().TemplateResponse(
        request,
        "pages/admin/ai_model_prices.html",
        context={
            "user": user,
            "active_page": "admin",
            "prices": [price_row(r, now) for r in rows],
            "total": total,
            "offset": offset,
            "limit": limit,
            "filters": filters,
            "page_query": urlencode({k: v for k, v in filters.items() if v}),
            "providers": await _service.providers(db),
            "currencies": await _currency_choices(db),
            "snapshot": _snapshot_info(),
            "now_input": utc_input(now.replace(second=0, microsecond=0)),
            "form": {},
            "notice": notice if notice in _NOTICES else "",
            "error": error if error in _ERRORS else "",
            "seed_counts": {"inserted": inserted, "closed": closed},
        },
    )


@router.get("/admin/ai-model-prices/model/", response_class=HTMLResponse)
async def admin_ai_model_price_history(
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
    provider: str = Query(""),
    model: str = Query(""),
    notice: str = Query(""),
    error: str = Query(""),
) -> Response:
    """One model's price history, with the "new price from" form."""
    provider_slug = normalize_provider(provider) or ""
    _hint, name = normalize_model_name(model)
    rows = await _service.history(db, provider_slug, name)
    if not rows:
        raise HTTPException(status_code=404, detail="No prices for this model")
    now = utcnow()
    resolved = await _service.resolve(db, name, now, provider=provider_slug)
    current = resolved.price if resolved is not None and resolved.matched_by == "model" else None
    template = current or rows[0]
    in_use = {r.id: (await price_usage_of(db, r.id)).count for r in rows}
    return get_templates().TemplateResponse(
        request,
        "pages/admin/ai_model_price_history.html",
        context={
            "user": user,
            "active_page": "admin",
            "provider": provider_slug,
            "model": name,
            "rows": [price_row(r, now) for r in rows],
            "in_use": in_use,
            "current_id": current.id if current else None,
            "form": {
                "currency": template.currency_code,
                "prices": {c: format_price(getattr(template, c)) or "" for c in PRICE_COLUMNS},
                "long_context_threshold": template.long_context_threshold or "",
                "aliases": " ".join(template.aliases or []),
            },
            "currencies": await _currency_choices(db),
            "now_input": utc_input(now.replace(second=0, microsecond=0)),
            "model_query": urlencode(_model_params(provider_slug, name)),
            "notice": notice if notice in _NOTICES else "",
            "error": error if error in _ERRORS else "",
        },
    )


@router.post("/admin/ai-model-prices/", response_model=None)
async def admin_ai_model_price_create(
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
    provider: str = Form(""),
    model: str = Form(""),
    currency: str = Form(""),
    effective_from: str = Form(""),
    effective_to: str = Form(""),
    long_context_threshold: str = Form(""),
    aliases: str = Form(""),
    note: str = Form(""),
    is_local: str = Form(""),
    baseline_model: str = Form(""),
    return_to: str = Form("list"),
    input: str = Form(""),
    output: str = Form(""),
    cache_write_5m: str = Form(""),
    cache_write_1h: str = Form(""),
    cache_read: str = Form(""),
    reasoning: str = Form(""),
    long_input: str = Form(""),
    long_output: str = Form(""),
    long_cache_write_5m: str = Form(""),
    long_cache_write_1h: str = Form(""),
    long_cache_read: str = Form(""),
) -> Response:
    """Add a price period ("new price from <instant>", or a past period with an end)."""
    provider_slug = normalize_provider(provider) or ""
    _hint, name = normalize_model_name(model)
    back = (_MODEL_URL, _model_params(provider_slug, name)) if return_to == "model" else (_BASE_URL, {})
    start = parse_utc(effective_from)
    end = parse_utc(effective_to) if effective_to.strip() else None
    if start is None or (effective_to.strip() and end is None):
        return _redirect(back[0], {**back[1], "error": "instant_invalid"}, anchor="new-price")
    threshold: int | None = None
    if long_context_threshold.strip():
        try:
            threshold = int(long_context_threshold.strip().replace("_", "").replace(",", ""))
        except ValueError:
            return _redirect(back[0], {**back[1], "error": "threshold_invalid"}, anchor="new-price")
    prices = {
        "input": input,
        "output": output,
        "cache_write_5m": cache_write_5m,
        "cache_write_1h": cache_write_1h,
        "cache_read": cache_read,
        "reasoning": reasoning,
        "long_input": long_input,
        "long_output": long_output,
        "long_cache_write_5m": long_cache_write_5m,
        "long_cache_write_1h": long_cache_write_1h,
        "long_cache_read": long_cache_read,
    }
    try:
        created = await _service.create(
            db,
            PriceInput(
                provider=provider,
                model=model,
                currency=currency,
                effective_from=start,
                effective_to=end,
                prices=prices,
                long_context_threshold=threshold,
                aliases=aliases,
                is_local=is_local == "1",
                baseline_model=baseline_model,
                note=note,
            ),
            actor=user,
            request=request,
        )
    except AppError as exc:
        return _redirect(back[0], {**back[1], "error": exc.code}, anchor="new-price")
    row = created.price
    return _redirect(
        _MODEL_URL, {**_model_params(row.provider, row.model), "notice": "closed" if created.closed else "created"}
    )


async def _row_or_back(db: AsyncSession, price_id: int) -> AiModelPrice | None:
    try:
        return await _service.get(db, price_id)
    except NotFoundError:
        return None


@router.post("/admin/ai-model-prices/{price_id}/active/", response_model=None)
async def admin_ai_model_price_toggle(
    price_id: int,
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
    active: str = Form(""),
) -> Response:
    """Deactivate or reactivate a price period."""
    row = await _row_or_back(db, price_id)
    if row is None:
        return _redirect(_BASE_URL, {"error": "not_found"})
    params = _model_params(row.provider, row.model)
    try:
        await _service.update(db, price_id, actor=user, request=request, is_active=active == "1")
    except AppError as exc:
        return _redirect(_MODEL_URL, {**params, "error": exc.code})
    return _redirect(_MODEL_URL, {**params, "notice": "activated" if active == "1" else "deactivated"})


@router.post("/admin/ai-model-prices/{price_id}/end/", response_model=None)
async def admin_ai_model_price_end(
    price_id: int,
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
    effective_to: str = Form(""),
) -> Response:
    """Set or clear the end of a period (empty reopens it)."""
    row = await _row_or_back(db, price_id)
    if row is None:
        return _redirect(_BASE_URL, {"error": "not_found"})
    params = _model_params(row.provider, row.model)
    end = parse_utc(effective_to) if effective_to.strip() else None
    if effective_to.strip() and end is None:
        return _redirect(_MODEL_URL, {**params, "error": "instant_invalid"})
    try:
        await _service.update(db, price_id, actor=user, request=request, effective_to=end)
    except AppError as exc:
        return _redirect(_MODEL_URL, {**params, "error": exc.code})
    return _redirect(_MODEL_URL, {**params, "notice": "ended"})


@router.post("/admin/ai-model-prices/{price_id}/details/", response_model=None)
async def admin_ai_model_price_details(
    price_id: int,
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
    aliases: str = Form(""),
    note: str = Form(""),
) -> Response:
    """Save a period's aliases and note."""
    row = await _row_or_back(db, price_id)
    if row is None:
        return _redirect(_BASE_URL, {"error": "not_found"})
    params = _model_params(row.provider, row.model)
    try:
        await _service.update(db, price_id, actor=user, request=request, aliases=aliases, note=note)
    except AppError as exc:
        return _redirect(_MODEL_URL, {**params, "error": exc.code})
    return _redirect(_MODEL_URL, {**params, "notice": "saved"})


@router.post("/admin/ai-model-prices/{price_id}/delete/", response_model=None)
async def admin_ai_model_price_delete(
    price_id: int,
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
) -> Response:
    """Delete an administrator price period no usage entry references."""
    row = await _row_or_back(db, price_id)
    if row is None:
        return _redirect(_BASE_URL, {"error": "not_found"})
    params = _model_params(row.provider, row.model)
    try:
        await _service.delete(db, price_id, actor=user, request=request)
    except AppError as exc:
        return _redirect(_MODEL_URL, {**params, "error": exc.code})
    if not await _service.history(db, row.provider, row.model):
        return _redirect(_BASE_URL, {"notice": "deleted"})
    return _redirect(_MODEL_URL, {**params, "notice": "deleted"})


@router.post("/admin/ai-model-prices/seed/", response_model=None)
async def admin_ai_model_price_seed(
    request: Request,
    user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = DB_SESSION,
) -> Response:
    """Load the prices bundled with this version."""
    try:
        outcome = await _service.load_seed(db, actor=user, request=request)
    except (SeedFormatError, OSError):
        return _redirect(_BASE_URL, {"error": "seed_invalid"}, anchor="bundled")
    if not outcome.changed:
        return _redirect(_BASE_URL, {"notice": "seed_unchanged"}, anchor="bundled")
    return _redirect(
        _BASE_URL,
        {
            "notice": "seeded",
            "inserted": str(outcome.inserted + outcome.history_inserted),
            "closed": str(outcome.closed),
        },
        anchor="bundled",
    )
