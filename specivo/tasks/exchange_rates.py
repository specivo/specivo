"""The optional daily exchange rate fetch.

Beat runs :func:`fetch_exchange_rates` every few hours; it returns at once
unless an administrator switched the fetch on (``exchange_rates_fetch_enabled``,
off by default), so an air-gapped install never tries the network. Failures are
recorded for the admin page and never retried before the next run: the ECB
publishes once a working day, so a missed run is caught up by the next one.
"""

from __future__ import annotations

import logging
from typing import Any

from specivo.tasks import celery_app
from specivo.tasks._async import run_async

logger = logging.getLogger(__name__)


@celery_app.task(ignore_result=True)
def fetch_exchange_rates() -> None:
    """Fetch the configured rate feed if the fetch is enabled."""
    try:
        run_async(fetch_exchange_rates_async())
    except Exception:
        logger.exception("Exchange rate fetch task failed")


async def fetch_exchange_rates_async(**fetch_kwargs: Any) -> None:
    from specivo.services.exchange_rate_service import ExchangeRateService
    from specivo.tasks._async import task_session

    async with task_session() as session:
        result = await ExchangeRateService().fetch_and_store(session, **fetch_kwargs)
        if result.error == "disabled":
            return
        await session.commit()
