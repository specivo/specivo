"""Load the AI model prices bundled with this version when a worker starts.

A new release may ship a refreshed LiteLLM snapshot. The seed loader is
append-only and idempotent, so running it at every worker start costs one
query when nothing changed and opens the new price periods after an upgrade.
An advisory lock keeps concurrent workers from inserting the same period.
Administrator prices are never touched.
"""

from __future__ import annotations

import logging

from celery.signals import worker_ready  # type: ignore[import-untyped]

from specivo.tasks import celery_app
from specivo.tasks._async import run_async

logger = logging.getLogger(__name__)


@celery_app.task(ignore_result=True)
def load_bundled_ai_model_prices() -> None:
    """Load the bundled prices (also run once at worker start)."""
    try:
        run_async(load_bundled_ai_model_prices_async())
    except Exception:
        logger.exception("Loading the bundled AI model prices failed")


async def load_bundled_ai_model_prices_async() -> None:
    from specivo.services.ai_model_price_service import AiModelPriceService
    from specivo.tasks._async import task_session

    async with task_session() as session:
        outcome = await AiModelPriceService().load_seed(session)
        await session.commit()
    if outcome.changed:
        logger.info(
            "Bundled AI model prices as of %s loaded: %d new periods, %d closed, %d first periods moved back",
            outcome.as_of.isoformat(),
            outcome.inserted + outcome.history_inserted,
            outcome.closed,
            outcome.rebased,
        )
    for message in outcome.skipped:
        logger.warning("Bundled AI model price skipped: %s", message)


@worker_ready.connect
def _load_bundled_prices_at_start(**_kwargs) -> None:  # type: ignore[no-untyped-def]
    try:
        run_async(load_bundled_ai_model_prices_async())
    except Exception as exc:  # noqa: BLE001 - a start-up step must not stop the worker
        logger.warning("Loading the bundled AI model prices at worker start failed: %s", exc)
