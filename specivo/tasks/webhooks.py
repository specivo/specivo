"""Celery tasks for outgoing webhooks: send one delivery, and sweep up pending ones.

The work itself lives in :mod:`specivo.services.webhook_delivery`; see
:mod:`specivo.services.webhook_events` for how deliveries are written.
"""

from __future__ import annotations

import logging

from specivo.tasks import celery_app
from specivo.tasks._async import run_async, task_session

logger = logging.getLogger(__name__)


@celery_app.task(ignore_result=True)
def deliver_webhook(delivery_id: int, attempts: int = 0) -> None:
    """Make the next attempt at one delivery; queue the retry when it is still pending.

    ``attempts`` is how many attempts the row had when this task was queued;
    a task that no longer matches the row does nothing.
    """
    from specivo.services.webhook_delivery import deliver

    async def _run():  # type: ignore[no-untyped-def]
        async with task_session() as session:
            return await deliver(session, delivery_id, attempts)

    outcome = run_async(_run())
    if outcome is None or outcome.retry_in is None:
        return
    try:
        deliver_webhook.apply_async(args=[delivery_id, outcome.attempts], countdown=outcome.retry_in, retry=False)
    except Exception as exc:  # noqa: BLE001 - the sweeper re-queues the row when it is due
        logger.warning("Could not queue retry of webhook delivery %d: %s", delivery_id, type(exc).__name__)


@celery_app.task(ignore_result=True)
def sweep_webhook_deliveries() -> int:
    """Queue pending deliveries that are due: lost at commit time, or a retry whose task was lost."""
    from specivo.services.webhook_delivery import claim_due
    from specivo.services.webhook_events import dispatch_deliveries

    async def _run():  # type: ignore[no-untyped-def]
        async with task_session() as session:
            return await claim_due(session)

    due = run_async(_run())
    if due:
        logger.info("Webhook sweeper queuing %d pending delivery(ies)", len(due))
    return dispatch_deliveries(due)
