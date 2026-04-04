"""Backfill search index: chunk all issues and wiki pages, then generate embeddings.

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


async def _backfill() -> None:
    from specivo.core.database import get_session_factory
    from specivo.models.attachment import Attachment
    from specivo.models.issue import Issue
    from specivo.models.journal import Journal
    from specivo.models.search import EmbeddingModel
    from specivo.models.wiki import WikiContent, WikiPage
    from specivo.services.chunking_service import ChunkingService
    from specivo.services.embedding_service import EmbeddingService

    chunker = ChunkingService()
    embedder = EmbeddingService()

    factory = get_session_factory()
    async with factory() as session:
        # Check for default embedding model
        model_result = await session.execute(select(EmbeddingModel).where(EmbeddingModel.is_default.is_(True)))
        model = model_result.scalar_one_or_none()
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

        logger.info(
            "Found %d issues, %d wiki pages, %d comments, %d attachments to index",
            issue_count,
            wiki_count,
            comment_count,
            attachment_count,
        )

        total = issue_count + wiki_count + comment_count + attachment_count
        if total == 0:
            logger.info("Nothing to backfill.")
            return

        start = time.time()
        total_chunks = 0
        total_embedded = 0
        processed = 0

        # Backfill issues
        offset = 0
        batch_size = 100
        while offset < issue_count:
            issues = (
                (await session.execute(select(Issue).order_by(Issue.id).offset(offset).limit(batch_size)))
                .scalars()
                .all()
            )

            for issue in issues:
                processed += 1
                chunks = chunker.chunk_issue(issue.subject, issue.description)
                if chunks:
                    try:
                        async with session.begin_nested():
                            await embedder.embed_source(session, "issue", issue.id, issue.project_id, chunks)
                            total_chunks += len(chunks)
                    except Exception as e:
                        logger.warning("  Skip issue %d: %s", issue.id, e)
                if processed % 10 == 0 or processed == total:
                    elapsed = time.time() - start
                    logger.info(
                        "  [%d/%d] %.0f%% — %d chunks (%.1fs)",
                        processed,
                        total,
                        processed / total * 100,
                        total_chunks,
                        elapsed,
                    )

            await session.flush()
            offset += batch_size

        # Backfill wiki pages
        offset = 0
        while offset < wiki_count:
            pages = (
                (await session.execute(select(WikiPage).order_by(WikiPage.id).offset(offset).limit(batch_size)))
                .scalars()
                .all()
            )

            for page in pages:
                processed += 1
                # Get latest content
                content_result = await session.execute(
                    select(WikiContent)
                    .where(WikiContent.page_id == page.id)
                    .order_by(WikiContent.version.desc())
                    .limit(1)
                )
                content = content_result.scalar_one_or_none()
                if content and content.text:
                    chunks = chunker.chunk_wiki_page(page.title, content.text)
                    if chunks:
                        from specivo.models.wiki import Wiki

                        wiki_result = await session.execute(select(Wiki.project_id).where(Wiki.id == page.wiki_id))
                        wiki_project_id = wiki_result.scalar_one_or_none() or 0
                        try:
                            async with session.begin_nested():
                                await embedder.embed_source(session, "wiki_page", page.id, wiki_project_id, chunks)
                                total_chunks += len(chunks)
                        except Exception as e:
                            logger.warning("  Skip wiki page %d: %s", page.id, e)
                if processed % 10 == 0 or processed == total:
                    elapsed = time.time() - start
                    logger.info(
                        "  [%d/%d] %.0f%% — %d chunks (%.1fs)",
                        processed,
                        total,
                        processed / total * 100,
                        total_chunks,
                        elapsed,
                    )

            await session.flush()
            offset += batch_size

        # Backfill comments (journals with notes)
        offset = 0
        while offset < comment_count:
            journals = (
                (
                    await session.execute(
                        select(Journal)
                        .where(Journal.notes.isnot(None), Journal.notes != "")
                        .order_by(Journal.id)
                        .offset(offset)
                        .limit(batch_size)
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
                            await embedder.embed_source(session, "journal", journal.id, journal.project_id, chunks)
                            total_chunks += len(chunks)
                    except Exception as e:
                        logger.warning("  Skip journal %d: %s", journal.id, e)
                if processed % 50 == 0 or processed == total:
                    elapsed = time.time() - start
                    logger.info(
                        "  [%d/%d] %.0f%% — %d chunks (%.1fs)",
                        processed,
                        total,
                        processed / total * 100,
                        total_chunks,
                        elapsed,
                    )

            await session.flush()
            offset += batch_size

        # Backfill attachments with descriptions
        offset = 0
        while offset < attachment_count:
            attachments = (
                (
                    await session.execute(
                        select(Attachment)
                        .where(Attachment.description.isnot(None), Attachment.description != "")
                        .order_by(Attachment.id)
                        .offset(offset)
                        .limit(batch_size)
                    )
                )
                .scalars()
                .all()
            )

            for att in attachments:
                processed += 1
                chunks = chunker.chunk_attachment(att.filename, att.description)
                if chunks:
                    if att.container_type == "Project":
                        project_id = att.container_id
                    elif att.container_type == "Issue":
                        issue_pid = await session.execute(select(Issue.project_id).where(Issue.id == att.container_id))
                        project_id = issue_pid.scalar_one_or_none()
                        if not project_id:
                            logger.warning("  Skip attachment %d: issue %d not found", att.id, att.container_id)
                            continue
                    else:
                        continue
                    try:
                        async with session.begin_nested():
                            await embedder.embed_source(session, "attachment", att.id, project_id, chunks)
                            total_chunks += len(chunks)
                    except Exception as e:
                        logger.warning("  Skip attachment %d: %s", att.id, e)
                if processed % 50 == 0 or processed == total:
                    elapsed = time.time() - start
                    logger.info(
                        "  [%d/%d] %.0f%% — %d chunks (%.1fs)",
                        processed,
                        total,
                        processed / total * 100,
                        total_chunks,
                        elapsed,
                    )

            await session.flush()
            offset += batch_size

        # Now generate embeddings for all chunks
        logger.info("Generating embeddings for %d chunks...", total_chunks)
        total_embedded = await embedder.backfill_model(session, model.id)

        await session.commit()

        elapsed = time.time() - start
        logger.info(
            "Done! Indexed %d entities → %d chunks → %d embeddings (%.1fs)",
            processed,
            total_chunks,
            total_embedded,
            elapsed,
        )


def main() -> None:
    asyncio.run(_backfill())


if __name__ == "__main__":
    main()
