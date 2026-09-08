"""Rebuild the wiki link graph and the search index after an import.

Both are suppressed while rows are being written — indexing every issue as it
arrives makes an import of any size crawl, and dispatching a link-graph task per
wiki page floods the queue — so they run once at the end over what was
imported.

Full-text search needs nothing here: its vectors are maintained by database
triggers, which fire on the same inserts either way. What is missing is the
chunk and embedding layer, which the application writes explicitly.
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from specivo.importers.core.ir import EntityType
from specivo.importers.core.pipeline import PhaseContext
from specivo.models.issue import Issue
from specivo.models.wiki import Wiki, WikiContent, WikiPage

logger = logging.getLogger(__name__)

# Entities indexed per flush. Embedding is the slow part of an import, so the
# batch is small enough to report progress at a useful rate.
_INDEX_BATCH = 25


async def rebuild_wiki_link_graph(ctx: PhaseContext) -> None:
    """Recompute which wiki page links to which, for imported pages.

    Run in-process rather than through the task queue: an import is usually run
    from a shell where no worker need be listening, and a dispatched task would
    then be silently dropped.
    """
    from specivo.services.wiki_link_service import WikiLinkService

    service = WikiLinkService()
    page_ids = list((await ctx.id_map.preload(ctx.session, EntityType.WIKI_PAGE)).values())
    if not page_ids:
        return

    rows = (await ctx.session.execute(select(WikiPage.id, WikiPage.wiki_id).where(WikiPage.id.in_(page_ids)))).all()
    for page_id, wiki_id in rows:
        try:
            await service.rebuild_page_links(ctx.session, wiki_id, page_id)
        except Exception as exc:  # noqa: BLE001 - a broken link must not end an import
            ctx.warn("Could not rebuild the link graph for a wiki page", page=page_id, reason=str(exc))
        ctx.tick()


async def backfill_search_index(ctx: PhaseContext) -> None:
    """Generate search chunks and embeddings for everything imported.

    Skipped on a dry run: it is the slowest phase and none of it would survive
    the rollback. Failure is reported rather than fatal — an import with an
    unindexed tail is recoverable with the existing backfill command, while a
    failed import is not.
    """
    if ctx.options.dry_run:
        logger.info("Dry run: skipping the search index backfill")
        return

    from specivo.schemas.search import SearchSourceType
    from specivo.services.chunking_service import ChunkingService
    from specivo.services.embedding_service import EmbeddingService

    chunker = ChunkingService()
    embedder = EmbeddingService()

    await _index_issues(ctx, chunker, embedder, SearchSourceType)
    await _index_wiki_pages(ctx, chunker, embedder, SearchSourceType)


async def _index_issues(ctx: PhaseContext, chunker, embedder, source_type) -> None:
    """Chunk and embed the issues this source has imported."""
    issue_ids = list((await ctx.id_map.preload(ctx.session, EntityType.ISSUE)).values())
    for start in range(0, len(issue_ids), _INDEX_BATCH):
        chunk_ids = issue_ids[start : start + _INDEX_BATCH]
        rows = (
            await ctx.session.execute(
                select(Issue.id, Issue.project_id, Issue.subject, Issue.description).where(Issue.id.in_(chunk_ids))
            )
        ).all()
        for issue_id, project_id, subject, description in rows:
            try:
                chunks = chunker.chunk_issue(subject, description)
                await embedder.embed_source(ctx.session, source_type.ISSUE, issue_id, project_id, chunks)
            except Exception as exc:  # noqa: BLE001 - see the docstring
                ctx.warn("Could not index an issue for search", issue=issue_id, reason=str(exc))
            ctx.tick()
        await ctx.session.flush()


async def _index_wiki_pages(ctx: PhaseContext, chunker, embedder, source_type) -> None:
    """Chunk and embed the wiki pages this source has imported."""
    page_ids = list((await ctx.id_map.preload(ctx.session, EntityType.WIKI_PAGE)).values())
    for start in range(0, len(page_ids), _INDEX_BATCH):
        chunk_ids = page_ids[start : start + _INDEX_BATCH]
        rows = (
            await ctx.session.execute(
                select(WikiPage.id, WikiPage.title, Wiki.project_id)
                .join(Wiki, Wiki.id == WikiPage.wiki_id)
                .where(WikiPage.id.in_(chunk_ids))
            )
        ).all()
        for page_id, title, project_id in rows:
            text = (
                await ctx.session.execute(
                    select(WikiContent.text)
                    .where(WikiContent.page_id == page_id)
                    .order_by(WikiContent.version.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            try:
                chunks = chunker.chunk_wiki_page(title, text or "")
                await embedder.embed_source(ctx.session, source_type.WIKI_PAGE, page_id, project_id, chunks)
            except Exception as exc:  # noqa: BLE001 - see the docstring
                ctx.warn("Could not index a wiki page for search", page=page_id, reason=str(exc))
            ctx.tick()
        await ctx.session.flush()
