"""AI usage tasks: recompute costs after a price change, and check the AI spend budgets.

``recompute_ai_usage_costs`` is started from the admin price page ("recompute
the affected range") or the REST route ``POST /api/v1/admin/ai-usage/recompute/``.
Each entry is re-priced at its own ``priced_at``; one audit event records the
totals before and after.

``check_ai_spend_budgets`` runs hourly from beat (``check-ai-spend-budgets``)
under a Redis lock, like the wiki trash purge: it records the budget
thresholds reached this month and mails the administrators and account
holders (:mod:`specivo.services.ai_spend_budget_service`).
"""

from __future__ import annotations

import logging
from datetime import date, datetime

from specivo.core.locks import beat_lock
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


_BUDGET_CHECK_LOCK_KEY = "specivo:check_ai_spend_budgets"
_BUDGET_CHECK_LOCK_TIMEOUT = 900  # seconds


@celery_app.task(bind=True, max_retries=1, default_retry_delay=60)
def check_ai_spend_budgets(self) -> None:
    """Alert at the thresholds the AI spend budgets reached this month (each once per month)."""
    try:
        run_async(check_ai_spend_budgets_async())
    except Exception as exc:  # noqa: BLE001
        logger.warning("AI spend budget check failed: %s", exc)
        raise self.retry(exc=exc)  # noqa: B904


async def check_ai_spend_budgets_async(today: date | None = None) -> int:
    """One run of the check under the beat lock: record the alerts, commit, then queue the mails.

    Returns how many alerts were sent (0 when another worker holds the lock).
    The mails are queued only after the alert rows are committed, so a run
    that fails before the commit sends nothing and the next run tries again.
    """
    from specivo.services.ai_spend_budget_service import AiSpendBudgetService
    from specivo.tasks._async import task_session

    async with beat_lock(_BUDGET_CHECK_LOCK_KEY, _BUDGET_CHECK_LOCK_TIMEOUT) as acquired:
        if not acquired:
            logger.debug("AI spend budget check skipped — another worker holds the lock")
            return 0
        service = AiSpendBudgetService()
        async with task_session() as session:
            outcome = await service.check(session, today=today)
            await session.commit()
        mails = service.notify(outcome.alerts)
    if outcome.alerts:
        logger.info(
            "AI spend budget check: %d budgets, %d alerts, %d mails queued",
            outcome.budgets,
            len(outcome.alerts),
            mails,
        )
    return len(outcome.alerts)
