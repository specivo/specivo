"""Backfill search index: chunk all issues and wiki pages, then generate embeddings.

Each batch of entities is committed independently so progress survives
crashes. Safe to re-run — existing sources are updated in place
(old chunks/embeddings are replaced).

Usage:
    python -m specivo.cli.backfill_embeddings
    # or via Makefile:
    make backfill-embeddings
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time

from sqlalchemy import func, select

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

# How many entities to process per committed batch.
BATCH_SIZE = 5


async def _backfill() -> None:
    from specivo.models.attachment import Attachment
    from specivo.models.issue import Issue
    from specivo.models.journal import Journal
    from specivo.models.search import EmbeddingModel
    from specivo.models.wiki import Wiki, WikiContent, WikiPage
    from specivo.schemas.search import SearchSourceType
    from specivo.services.chunking_service import ChunkingService
    from specivo.services.embedding_service import EmbeddingService

    chunker = ChunkingService()
    embedder = EmbeddingService()

    # Import factory late to allow env/config setup
    from specivo.core.database import get_session_factory

    factory = get_session_factory()

    # Verify embedding model exists
    async with factory() as session:
        model = (
            await session.execute(select(EmbeddingModel).where(EmbeddingModel.is_default.is_(True)))
        ).scalar_one_or_none()
        if model is None:
            logger.error("No default embedding model found. Run 'make seed' first.")
            sys.exit(1)

        logger.info("Embedding model: %s (provider=%s, dims=%d)", model.name, model.provider, model.dimensions)
        # Count entities
        issue_count = (await session.execute(select(func.count()).select_from(Issue))).scalar_one()
        wiki_count = (await session.execute(select(func.count()).select_from(WikiPage))).scalar_one()
        comment_count = (
            await session.execute(
                select(func.count()).select_from(Journal).where(Journal.notes.isnot(None), Journal.notes != "")
            )
        ).scalar_one()
        attachment_count = (
            await session.execute(
                select(func.count())
                .select_from(Attachment)
                .where(Attachment.description.isnot(None), Attachment.description != "")
            )
        ).scalar_one()

    total = issue_count + wiki_count + comment_count + attachment_count
    logger.info(
        "Found %d issues, %d wiki pages, %d comments, %d attachments to index",
        issue_count,
        wiki_count,
        comment_count,
        attachment_count,
    )
    if total == 0:
        logger.info("Nothing to backfill.")
        return

    start = time.time()
    total_chunks = 0
    processed = 0

    def _log_progress() -> None:
        elapsed = time.time() - start
        logger.info(
            "  [%d/%d] %.0f%% — %d chunks (%.1fs)",
            processed,
            total,
            processed / total * 100,
            total_chunks,
            elapsed,
        )

    # --- Issues ---
    for offset in range(0, issue_count, BATCH_SIZE):
        async with factory() as session:
            issues = (
                (await session.execute(select(Issue).order_by(Issue.id).offset(offset).limit(BATCH_SIZE)))
                .scalars()
                .all()
            )

            for issue in issues:
                processed += 1
                chunks = chunker.chunk_issue(issue.subject, issue.description)
                if chunks:
                    try:
                        async with session.begin_nested():
                            await embedder.embed_source(
                                session, SearchSourceType.ISSUE, issue.id, issue.project_id, chunks
                            )
                            total_chunks += len(chunks)
                    except Exception as e:
                        logger.warning("  Skip issue %d: %s", issue.id, e)

            await session.commit()
        if processed % 20 == 0 or offset + BATCH_SIZE >= issue_count:
            _log_progress()

    # --- Wiki pages ---
    for offset in range(0, wiki_count, BATCH_SIZE):
        async with factory() as session:
            pages = (
                (await session.execute(select(WikiPage).order_by(WikiPage.id).offset(offset).limit(BATCH_SIZE)))
                .scalars()
                .all()
            )

            for page in pages:
                processed += 1
                content = (
                    await session.execute(
                        select(WikiContent)
                        .where(WikiContent.page_id == page.id)
                        .order_by(WikiContent.version.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if content and content.text:
                    chunks = chunker.chunk_wiki_page(page.title, content.text)
                    if chunks:
                        wiki_project_id = (
                            await session.execute(select(Wiki.project_id).where(Wiki.id == page.wiki_id))
                        ).scalar_one_or_none() or 0
                        try:
                            async with session.begin_nested():
                                await embedder.embed_source(
                                    session,
                                    SearchSourceType.WIKI_PAGE,
                                    page.id,
                                    wiki_project_id,
                                    chunks,
                                )
                                total_chunks += len(chunks)
                        except Exception as e:
                            logger.warning("  Skip wiki page %d: %s", page.id, e)

            await session.commit()
        if processed % 20 == 0 or offset + BATCH_SIZE >= wiki_count:
            _log_progress()

    # --- Comments (journals with notes) ---
    for offset in range(0, comment_count, BATCH_SIZE):
        async with factory() as session:
            journals = (
                (
                    await session.execute(
                        select(Journal)
                        .where(Journal.notes.isnot(None), Journal.notes != "")
                        .order_by(Journal.id)
                        .offset(offset)
                        .limit(BATCH_SIZE)
                    )
                )
                .scalars()
                .all()
            )

            for journal in journals:
                processed += 1
                chunks = chunker.chunk_journal(journal.notes)
                if chunks:
                    try:
                        async with session.begin_nested():
                            await embedder.embed_source(
                                session,
                                SearchSourceType.JOURNAL,
                                journal.id,
                                journal.project_id,
                                chunks,
                            )
                            total_chunks += len(chunks)
                    except Exception as e:
                        logger.warning("  Skip journal %d: %s", journal.id, e)

            await session.commit()
        if processed % 50 == 0 or offset + BATCH_SIZE >= comment_count:
            _log_progress()

    # --- Attachments with descriptions ---
    for offset in range(0, attachment_count, BATCH_SIZE):
        async with factory() as session:
            attachments = (
                (
                    await session.execute(
                        select(Attachment)
                        .where(Attachment.description.isnot(None), Attachment.description != "")
                        .order_by(Attachment.id)
                        .offset(offset)
                        .limit(BATCH_SIZE)
                    )
                )
                .scalars()
                .all()
            )

            for att in attachments:
                processed += 1
                if att.container_type == "Project":
                    project_id = att.container_id
                elif att.container_type == "Issue":
                    pid_result = await session.execute(select(Issue.project_id).where(Issue.id == att.container_id))
                    project_id = pid_result.scalar_one_or_none()
                    if not project_id:
                        logger.warning("  Skip attachment %d: issue %d not found", att.id, att.container_id)
                        continue
                else:
                    continue

                meta = getattr(att, "metadata", None)
                extracted_text = meta.get("extracted_text") if meta else None
                ai_description = meta.get("ai_description") if meta else None
                description = att.description
                if ai_description:
                    description = f"{description}\n\n{ai_description}" if description else ai_description

                chunks = chunker.chunk_attachment(att.filename, description, extracted_text=extracted_text)
                if chunks:
                    try:
                        async with session.begin_nested():
                            await embedder.embed_source(
                                session, SearchSourceType.ATTACHMENT, att.id, project_id, chunks
                            )
                            total_chunks += len(chunks)
                    except Exception as e:
                        logger.warning("  Skip attachment %d: %s", att.id, e)

            await session.commit()
        if processed % 50 == 0 or offset + BATCH_SIZE >= attachment_count:
            _log_progress()

    elapsed = time.time() - start
    logger.info("Re-indexed %d entities → %d chunks+embeddings (%.1fs)", processed, total_chunks, elapsed)


def main() -> None:
    asyncio.run(_backfill())


if __name__ == "__main__":
    main()
