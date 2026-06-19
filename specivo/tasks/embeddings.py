"""Celery tasks for async embedding generation.

Tasks:
- generate_embeddings: Chunk content and generate embeddings for a single entity.
- backfill_model_embeddings: Generate embeddings for all existing chunks using a new model.
"""

from __future__ import annotations

import logging

from specivo.core.constants import CELERY_MAX_RETRIES, CELERY_RETRY_DELAY_EMBEDDING
from specivo.schemas.search import SearchSourceType
from specivo.tasks import celery_app
from specivo.tasks._async import run_async, task_session

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, max_retries=CELERY_MAX_RETRIES, default_retry_delay=CELERY_RETRY_DELAY_EMBEDDING)
def generate_embeddings(self, source_type: str, entity_id: int, project_id: int) -> None:  # type: ignore[no-untyped-def]
    """Chunk content and generate embeddings for a source entity.

    Called on issue/wiki/journal create/update.

    Args:
        source_type: One of :class:`SearchSourceType` string values
            (``"issue"``, ``"wiki_page"``, ``"journal"``, ``"attachment"``).
            Passed as a plain string because Celery serializes task args
            to JSON via the broker.
        entity_id: ID of the source entity.
        project_id: ID of the project the entity belongs to.
    """
    try:
        run_async(_generate_embeddings_async(source_type, entity_id, project_id))
    except Exception as exc:
        logger.warning("Failed to generate embeddings for %s:%d: %s", source_type, entity_id, exc)
        raise self.retry(exc=exc)


async def _generate_embeddings_async(source_type: str, entity_id: int, project_id: int) -> None:
    """Async implementation of embedding generation."""
    from specivo.services.chunking_service import ChunkingService
    from specivo.services.embedding_service import EmbeddingService

    chunking = ChunkingService()
    embedding = EmbeddingService()

    async with task_session() as session:
        chunks: list[str] = []

        if source_type == SearchSourceType.ISSUE:
            from sqlalchemy import select

            from specivo.models.issue import Issue

            issue_result = await session.execute(select(Issue).where(Issue.id == entity_id))
            issue = issue_result.scalar_one_or_none()
            if issue:
                chunks = chunking.chunk_issue(issue.subject, issue.description)

        elif source_type == SearchSourceType.WIKI_PAGE:
            from sqlalchemy import select

            from specivo.models.wiki import WikiContent, WikiPage

            page_result = await session.execute(select(WikiPage).where(WikiPage.id == entity_id))
            page = page_result.scalar_one_or_none()
            if page:
                # Get latest content version
                content_result = await session.execute(
                    select(WikiContent)
                    .where(WikiContent.page_id == page.id)
                    .order_by(WikiContent.version.desc())
                    .limit(1)
                )
                content = content_result.scalar_one_or_none()
                if content:
                    chunks = chunking.chunk_wiki_page(page.title, content.text)

        elif source_type == SearchSourceType.JOURNAL:
            from sqlalchemy import select

            from specivo.models.journal import Journal

            journal_result = await session.execute(select(Journal).where(Journal.id == entity_id))
            journal = journal_result.scalar_one_or_none()
            if journal and journal.notes:
                chunks = chunking.chunk_journal(journal.notes)

        elif source_type == SearchSourceType.ATTACHMENT:
            from sqlalchemy import select

            from specivo.models.attachment import Attachment

            att_result = await session.execute(select(Attachment).where(Attachment.id == entity_id))
            att = att_result.scalar_one_or_none()
            if att:
                # Extract metadata content fields if available
                meta: dict | None = getattr(att, "metadata", None)
                extracted_text: str | None = None
                ai_description: str | None = None
                if meta:
                    extracted_text = meta.get("extracted_text")
                    ai_description = meta.get("ai_description")

                description = att.description
                if ai_description:
                    description = f"{description}\n\n{ai_description}" if description else ai_description

                chunks = chunking.chunk_attachment(
                    att.filename,
                    description,
                    extracted_text=extracted_text,
                )

        if chunks:
            await embedding.embed_source(session, source_type, entity_id, project_id, chunks)
            await session.commit()
            logger.info("Generated embeddings for %s:%d (%d chunks)", source_type, entity_id, len(chunks))


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
