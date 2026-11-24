"""Celery tasks that keep suggested links (``similar`` edges) in step with the embeddings.

Tasks:
- compute_similar_links: recompute the open suggestions of a project, or of
  the issues and wiki pages just embedded again (queued by the embedding task).
- sweep_similar_links (beat, daily): queue a full computation for every
  project whose issues or pages carry embeddings of the default model.

Both run on the default queue: they read stored vectors and never load the
embedding model.
"""

from __future__ import annotations

import logging

from specivo.core.constants import CELERY_MAX_RETRIES, CELERY_RETRY_DELAY_LINK_GRAPH
from specivo.core.locks import beat_lock
from specivo.tasks import celery_app
from specivo.tasks._async import run_async, task_session

logger = logging.getLogger(__name__)

_SWEEP_LOCK_KEY = "specivo:sweep_similar_links"
_SWEEP_LOCK_TIMEOUT = 300  # seconds; a tick only lists projects and queues tasks


@celery_app.task(bind=True, max_retries=CELERY_MAX_RETRIES, default_retry_delay=CELERY_RETRY_DELAY_LINK_GRAPH)
def compute_similar_links(self, project_id: int | None = None, items: list[list] | None = None) -> int:
    """Recompute suggested links: the whole of *project_id*, or only *items* (``[type, id]`` pairs).

    Idempotent: open suggestions are replaced, decided ones are kept, so a
    retry or a duplicate message redoes the same work. Returns how many
    suggestions were stored.
    """
    keys = [(str(t), int(i)) for t, i in items or []]
    try:
        stored: int = run_async(_compute_async(project_id, keys))
        return stored
    except Exception as exc:
        logger.warning("Suggested links computation failed (project_id=%s, %d item(s)): %s", project_id, len(keys), exc)
        raise self.retry(exc=exc) from exc


async def _compute_async(project_id: int | None, items: list[tuple[str, int]]) -> int:
    from specivo.services.link_suggestion_service import compute_for_items, compute_for_project

    async with task_session() as session:
        if project_id is not None:
            stored = await compute_for_project(session, project_id)
        else:
            stored = await compute_for_items(session, items)
        await session.commit()
    return stored


@celery_app.task
def sweep_similar_links() -> list[int]:
    """Queue a full suggested links computation for every project with embeddings (beat, daily)."""
    queued: list[int] = run_async(_sweep_async())
    return queued


async def _sweep_async() -> list[int]:
    from specivo.services.link_suggestion_service import (
        default_model_id,
        dispatch_compute,
        projects_with_vectors_or_embeddings,
    )

    async with beat_lock(_SWEEP_LOCK_KEY, _SWEEP_LOCK_TIMEOUT) as acquired:
        if not acquired:
            logger.debug("Suggested links sweep skipped: another worker holds the lock")
            return []
        async with task_session() as session:
            model_id = await default_model_id(session)
            if model_id is None:
                return []
            project_ids = await projects_with_vectors_or_embeddings(session, model_id)
    return [project_id for project_id in project_ids if dispatch_compute(project_id=project_id)]
