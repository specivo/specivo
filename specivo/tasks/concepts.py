"""Celery tasks for concept extraction with a project's own AI provider.

Tasks:
- extract_concepts: one run for a project (changed sources only, up to its run
  limit). Queued from ``POST /projects/{key}/concept-extraction/run/``, by the
  sweep and by ``python -m specivo.cli.extract_concepts``.
- sweep_concept_extraction (beat, daily): queue a run for every project that
  has the extraction switched on and a provider.

A run is never retried automatically: it calls a paid or rate-limited
provider, and whatever it did not finish is picked up by the next run (only
changed sources are sent). One run per project at a time (a Redis lock).
"""

from __future__ import annotations

import logging
from typing import Any

from specivo.core.locks import beat_lock
from specivo.tasks import celery_app
from specivo.tasks._async import run_async, task_session

logger = logging.getLogger(__name__)

_SWEEP_LOCK_KEY = "specivo:sweep_concept_extraction"
_SWEEP_LOCK_TIMEOUT = 300  # seconds; a tick only lists projects and queues tasks
# Long enough for a run of the largest run limit against a slow local model.
_RUN_LOCK_TIMEOUT = 6 * 3600


def run_lock_key(project_id: int) -> str:
    return f"specivo:concept_extraction:{project_id}"


@celery_app.task
def extract_concepts(project_id: int) -> dict[str, Any]:
    """Run concept extraction for one project; returns what the run did."""
    result: dict[str, Any] = run_async(_extract_async(project_id))
    return result


async def _extract_async(project_id: int) -> dict[str, Any]:
    from specivo.services.concept_extraction import extract_project

    async with beat_lock(run_lock_key(project_id), _RUN_LOCK_TIMEOUT) as acquired:
        if not acquired:
            logger.info("Concept extraction of project %d is already running; skipped", project_id)
            return {"status": "busy"}
        async with task_session() as session:
            try:
                result = await extract_project(session, project_id)
            except Exception:
                await session.rollback()
                logger.exception("Concept extraction of project %d failed", project_id)
                raise
    return result.as_dict()


@celery_app.task
def sweep_concept_extraction() -> list[int]:
    """Queue a run for every project with concept extraction on (beat, daily)."""
    queued: list[int] = run_async(_sweep_async())
    return queued


async def _sweep_async() -> list[int]:
    from specivo.services.concept_extraction import dispatch_extract, enabled_project_ids

    async with beat_lock(_SWEEP_LOCK_KEY, _SWEEP_LOCK_TIMEOUT) as acquired:
        if not acquired:
            logger.debug("Concept extraction sweep skipped: another worker holds the lock")
            return []
        async with task_session() as session:
            project_ids = await enabled_project_ids(session)
    return [project_id for project_id in project_ids if dispatch_extract(project_id)]
