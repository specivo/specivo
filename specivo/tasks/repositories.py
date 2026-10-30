"""Celery tasks for incoming repository webhooks: process one delivery, and sweep up lost ones.

The work itself lives in :mod:`specivo.services.repository_hooks`.
"""

from __future__ import annotations

import logging

from specivo.tasks import celery_app
from specivo.tasks._async import run_async, task_session

logger = logging.getLogger(__name__)


@celery_app.task(ignore_result=True)
def process_repository_delivery(delivery_id: int) -> None:
    """Link the commits of one accepted delivery to issues; does nothing for any other state."""
    from specivo.services.repository_hooks import process_delivery

    async def _run():  # type: ignore[no-untyped-def]
        async with task_session() as session:
            await process_delivery(session, delivery_id)
            await session.commit()

    run_async(_run())


@celery_app.task(ignore_result=True)
def sweep_repository_deliveries() -> int:
    """Queue accepted deliveries whose task was lost (broker down when the forge called)."""
    from specivo.services.repository_hooks import claim_stale, queue_processing

    async def _run():  # type: ignore[no-untyped-def]
        async with task_session() as session:
            return await claim_stale(session)

    stale = run_async(_run())
    if stale:
        logger.info("Repository delivery sweeper queuing %d delivery(ies)", len(stale))
    return queue_processing(stale)
