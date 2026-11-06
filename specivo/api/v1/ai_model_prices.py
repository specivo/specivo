"""AI model prices API (administrators): the price history, resolution and the bundled seed."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import AwareDatetime

from specivo.api.v1.admin import require_admin_api
from specivo.core.database import DbSession
from specivo.core.utils import utcnow
from specivo.models.user import User
from specivo.schemas.ai_model_price import (
    AiModelPriceCreate,
    AiModelPriceCreatedOut,
    AiModelPriceOut,
    AiModelPriceResolveOut,
    AiModelPriceSeedOut,
    AiModelPriceUpdate,
    prices_dict,
)
from specivo.schemas.common import PaginatedResponse
from specivo.services.ai_model_price_service import UNSET, AiModelPriceService, PriceInput

router = APIRouter(prefix="/admin/ai-model-prices", tags=["ai-model-prices"])
_service = AiModelPriceService()


@router.get("/", response_model=PaginatedResponse[AiModelPriceOut])
async def list_prices(
    db: DbSession,
    current_user: User = Depends(require_admin_api),
    provider: str | None = Query(None, max_length=50),
    q: str | None = Query(None, max_length=200, description="Part of a model name or alias"),
    source: Literal["seed", "admin"] | None = Query(None),
    status: Literal["active", "inactive", "all"] = Query("active"),
    currency: str | None = Query(None, min_length=3, max_length=3),
    valid_at: AwareDatetime | None = Query(None, description="Only periods that contain this instant"),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
) -> PaginatedResponse[AiModelPriceOut]:
    """Price periods by provider and model, newest period first (admin only)."""
    rows, total = await _service.list_prices(
        db,
        provider=provider,
        q=q,
        source=source,
        status=status,
        currency=currency,
        valid_at=valid_at,
        offset=offset,
        limit=limit,
    )
    return PaginatedResponse[AiModelPriceOut](
        total_count=total, offset=offset, limit=limit, items=[AiModelPriceOut.from_row(r) for r in rows]
    )


@router.post("/", response_model=AiModelPriceCreatedOut, status_code=201)
async def create_price(
    data: AiModelPriceCreate,
    request: Request,
    db: DbSession,
    current_user: User = Depends(require_admin_api),
) -> AiModelPriceCreatedOut:
    """Add a price period (admin only). Audited.

    "New price from ``effective_from``": without ``effective_to`` the open admin
    period of the model is closed at that instant. Past periods carry an
    ``effective_to``. The currency must be enabled. Errors: 409
    ``ai_model_price_overlap``, 409 ``ai_model_price_in_use``, 422
    ``ai_model_price_invalid``, ``currency_unknown``, ``currency_inactive``.
    """
    created = await _service.create(
        db,
        PriceInput(
            provider=data.provider,
            model=data.model,
            currency=data.currency,
            prices=prices_dict(data.prices),
            effective_from=data.effective_from,
            effective_to=data.effective_to,
            long_context_threshold=data.long_context_threshold,
            aliases=data.aliases,
            is_local=data.is_local,
            baseline_model=data.baseline_model,
            note=data.note,
        ),
        actor=current_user,
        request=request,
    )
    await db.refresh(created.price)
    out = AiModelPriceOut.from_row(created.price).model_dump()
    return AiModelPriceCreatedOut(**out, closed_price_id=created.closed.id if created.closed else None)


@router.get("/providers/", response_model=list[str])
async def list_providers(db: DbSession, current_user: User = Depends(require_admin_api)) -> list[str]:
    """Providers that have at least one price (admin only)."""
    return await _service.providers(db)


@router.get("/history/", response_model=list[AiModelPriceOut])
async def price_history(
    db: DbSession,
    current_user: User = Depends(require_admin_api),
    provider: str = Query(..., max_length=50),
    model: str = Query(..., max_length=250),
) -> list[AiModelPriceOut]:
    """Every period of one model, seed and admin, active and not, newest first (admin only)."""
    return [AiModelPriceOut.from_row(r) for r in await _service.history(db, provider, model)]


@router.get("/resolve/", response_model=AiModelPriceResolveOut)
async def resolve_price(
    db: DbSession,
    current_user: User = Depends(require_admin_api),
    model: str = Query(..., max_length=250),
    provider: str | None = Query(None, max_length=50),
    at: AwareDatetime | None = Query(None, description="The instant to price at; default now"),
) -> AiModelPriceResolveOut:
    """Which price applies to a model at an instant (admin only).

    ``unpriced`` when no active period contains the instant: there is never a
    fallback to the latest price.
    """
    instant: datetime = at or utcnow()
    resolved = await _service.resolve(db, model, instant, provider=provider)
    return AiModelPriceResolveOut.build(model, instant, resolved)


@router.post("/seed/", response_model=AiModelPriceSeedOut)
async def load_seed(
    request: Request, db: DbSession, current_user: User = Depends(require_admin_api)
) -> AiModelPriceSeedOut:
    """Load the prices bundled with this version (admin only). Audited when something changed.

    Append-only and idempotent: new models open a period at the snapshot's
    ``as_of``, changed prices close the open seed period there and open a new
    one, administrator prices are never touched.
    """
    outcome = await _service.load_seed(db, actor=current_user, request=request)
    return AiModelPriceSeedOut.from_outcome(outcome)


@router.get("/{price_id}/", response_model=AiModelPriceOut)
async def get_price(price_id: int, db: DbSession, current_user: User = Depends(require_admin_api)) -> AiModelPriceOut:
    return AiModelPriceOut.from_row(await _service.get(db, price_id))


@router.patch("/{price_id}/", response_model=AiModelPriceOut)
async def update_price(
    price_id: int,
    data: AiModelPriceUpdate,
    request: Request,
    db: DbSession,
    current_user: User = Depends(require_admin_api),
) -> AiModelPriceOut:
    """Change a period's end, active flag, aliases or note (admin only). Audited.

    Prices, the currency and the start cannot change: enter a new price.
    Deactivate a price instead of deleting it when usage entries reference it.
    """
    sent = data.model_fields_set
    row = await _service.update(
        db,
        price_id,
        actor=current_user,
        request=request,
        effective_to=data.effective_to if "effective_to" in sent else UNSET,
        is_active=data.is_active,
        aliases=data.aliases if "aliases" in sent and data.aliases is not None else UNSET,
        note=data.note if "note" in sent else UNSET,
    )
    await db.refresh(row)
    return AiModelPriceOut.from_row(row)


@router.delete("/{price_id}/", status_code=204)
async def delete_price(
    price_id: int, request: Request, db: DbSession, current_user: User = Depends(require_admin_api)
) -> Response:
    """Delete an administrator price that no usage entry references (admin only). Audited.

    Bundled (seed) prices are deactivated instead (422
    ``ai_model_price_not_deletable``); a referenced price is 409
    ``ai_model_price_in_use``.
    """
    await _service.delete(db, price_id, actor=current_user, request=request)
    return Response(status_code=204)
