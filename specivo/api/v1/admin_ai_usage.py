"""AI usage administration API: recompute the costs of entries in a range after price changes."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from specivo.api.v1.admin import require_admin_api
from specivo.core.database import DbSession
from specivo.models.user import User
from specivo.schemas.ai_usage import AiUsageRecomputeIn, AiUsageRecomputeOut
from specivo.services.ai_usage_service import AiUsageService

router = APIRouter(prefix="/admin/ai-usage", tags=["ai-usage"])
_service = AiUsageService()


@router.post("/recompute/", response_model=AiUsageRecomputeOut, status_code=202)
async def recompute_costs(
    data: AiUsageRecomputeIn,
    db: DbSession,
    current_user: User = Depends(require_admin_api),
) -> AiUsageRecomputeOut:
    """Re-price the AI usage entries priced in ``[from, to)`` in the background (admin only).

    Each entry is resolved at its own ``priced_at``, never at the latest
    price; changed entries get a new ``revision``. One
    ``ai_usage_costs_recomputed`` audit event records the totals before and
    after. Errors: 422 ``ai_usage_invalid``, 503 ``queue_unavailable``.
    """
    entries = await _service.count_in_range(
        db, start=data.start, end=data.end, provider=data.provider, model=data.model
    )
    task_id = _service.queue_recompute(
        actor_id=current_user.id, start=data.start, end=data.end, provider=data.provider, model=data.model
    )
    return AiUsageRecomputeOut(task_id=task_id, entries=entries)
