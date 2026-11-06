"""Recompute the costs of AI usage entries in a range, after an administrator changed prices.

Started from the admin price page ("recompute the affected range") or the
REST route ``POST /api/v1/admin/ai-usage/recompute/``. Each entry is
re-priced at its own ``priced_at``; one audit event records the totals before
and after.
"""

from __future__ import annotations

import logging
from datetime import datetime

from specivo.tasks import celery_app
from specivo.tasks._async import run_async

logger = logging.getLogger(__name__)


@celery_app.task(ignore_result=True)
def recompute_ai_usage_costs(
    actor_id: int | None, start: str, end: str, provider: str | None = None, model: str | None = None
) -> None:
    """Re-price the entries whose ``priced_at`` lies in ``[start, end)`` (ISO instants)."""
    try:
        run_async(recompute_ai_usage_costs_async(actor_id, start, end, provider, model))
    except Exception:
        logger.exception("Recomputing AI usage costs failed")


async def recompute_ai_usage_costs_async(
    actor_id: int | None, start: str, end: str, provider: str | None = None, model: str | None = None
) -> None:
    from specivo.services.ai_usage_service import AiUsageService
    from specivo.tasks._async import task_session

    async with task_session() as session:
        outcome = await AiUsageService().recompute_range(
            session,
            actor_id=actor_id,
            start=datetime.fromisoformat(start),
            end=datetime.fromisoformat(end),
            provider=provider,
            model=model,
        )
        await session.commit()
    logger.info("Recomputed AI usage costs: %d entries read, %d changed", outcome.entries, outcome.changed)
