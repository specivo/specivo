"""Celery tasks for async embedding generation.

Tasks:
- embed_search_sources: Embed the chunks of the given sources that have no embedding yet.
  Queued after every commit that writes chunks (see ``search_index_service``).
- backfill_model_embeddings: Generate embeddings for all existing chunks using a new model.
"""

from __future__ import annotations

import logging

from celery.signals import worker_ready  # type: ignore[import-untyped]

from specivo.core.constants import CELERY_MAX_RETRIES, CELERY_RETRY_DELAY_EMBEDDING
from specivo.tasks import celery_app
from specivo.tasks._async import run_async, task_session

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True, ignore_result=True, max_retries=CELERY_MAX_RETRIES, default_retry_delay=CELERY_RETRY_DELAY_EMBEDDING
)
def embed_search_sources(self, sources: list[list]) -> None:  # type: ignore[no-untyped-def]
    """Embed the chunks of *sources* that have no embedding for the default model.

    Idempotent: a source deleted or already embedded since the task was queued
    is skipped, so a duplicate or late task costs one query per source. Does
    nothing when no embedding model can run in this worker.

    Args:
        sources: ``[source_type, entity_id]`` pairs, with source_type one of the
            :class:`~specivo.schemas.search.SearchSourceType` values. Lists
            rather than tuples because Celery serializes task args to JSON.
    """
    try:
        run_async(_embed_sources_async([(str(t), int(i)) for t, i in sources]))
    except Exception as exc:
        logger.warning("Embedding task failed for %d search source(s): %s", len(sources), exc)
        raise self.retry(exc=exc)


async def _embed_sources_async(sources: list[tuple[str, int]]) -> None:
    """Embed each source in its own transaction, so one failure loses only that source."""
    from specivo.services.embedding_service import EmbeddingService

    service = EmbeddingService()

    async with task_session() as session:
        model = await service.get_default_model(session)
        if model is None or not service.is_model_usable(model):
            logger.debug("No usable embedding model; skipping %d search source(s)", len(sources))
            return
        model_id = model.id

        for source_type, entity_id in sources:
            try:
                count = await service.embed_pending(session, [(source_type, entity_id)], model_id=model_id)
                await session.commit()
            except Exception as exc:  # noqa: BLE001 - one bad source must not block the rest
                await session.rollback()
                logger.warning("Embedding failed for %s %d: %s", source_type, entity_id, exc)
                continue
            if count:
                logger.info("Embedded %d chunk(s) for %s %d", count, source_type, entity_id)


@worker_ready.connect
def _log_embedding_model_status(**_kwargs) -> None:  # type: ignore[no-untyped-def]
    """Say once, at worker start, whether semantic indexing can run here."""
    try:
        run_async(_log_model_status_async())
    except Exception as exc:  # noqa: BLE001 - a status line must not stop the worker
        logger.info("Could not check the embedding model at worker start: %s", exc)


async def _log_model_status_async() -> None:
    from specivo.services.embedding_service import EmbeddingService

    service = EmbeddingService()
    async with task_session() as session:
        model = await service.get_default_model(session)
    if model is None:
        logger.info("Semantic indexing is off: no default embedding model is registered")
    elif service.is_model_usable(model):
        logger.info("Semantic indexing is on: embedding with %s", model.name)
    else:
        logger.info(
            "Semantic indexing is off: the files for embedding model %s are not in data/models; "
            "keyword search is unaffected",
            model.name,
        )


@celery_app.task(bind=True, max_retries=1, default_retry_delay=60)
def backfill_model_embeddings(self, model_id: int) -> None:  # type: ignore[no-untyped-def]
    """Generate embeddings for all existing chunks using a new model.

    Args:
        model_id: ID of the EmbeddingModel to backfill.
    """
    try:
        run_async(_backfill_async(model_id))
    except Exception as exc:
        logger.warning("Failed to backfill model %d: %s", model_id, exc)
        raise self.retry(exc=exc)


async def _backfill_async(model_id: int) -> None:
    """Async implementation of model backfill."""
    from specivo.services.embedding_service import EmbeddingService

    embedding = EmbeddingService()

    async with task_session() as session:
        count = await embedding.backfill_model(session, model_id)
        await session.commit()
        logger.info("Backfill complete: %d embeddings for model %d", count, model_id)
