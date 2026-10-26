"""Rebuild the wiki link graph and the search index after an import.

Both are suppressed while rows are being written — indexing every issue as it
arrives makes an import of any size crawl, and dispatching a link-graph task per
wiki page floods the queue — so they run once at the end over what was
imported.

Full-text search on issues and wiki pages needs nothing here: their vectors are
maintained by database triggers. Comments and attachments are searched through
their chunks, which the import sessions do not write as rows arrive (see
``defer_search_index``), so this phase writes them for every imported source.
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from specivo.importers.core.ir import EntityType
from specivo.importers.core.pipeline import PhaseContext
from specivo.models.wiki import WikiPage

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
    """Write search chunks for everything imported, and embed them when a model can run.

    Covers issues, wiki pages, comments (honouring the comment settings) and
    attachments. Embedding runs inline because an import is usually run from a
    shell where no worker need be listening.

    Skipped on a dry run: it is the slowest phase and none of it would survive
    the rollback. Failure is reported rather than fatal — an import with an
    unindexed tail is recoverable with the existing backfill command, while a
    failed import is not.
    """
    if ctx.options.dry_run:
        logger.info("Dry run: skipping the search index backfill")
        return

    from specivo.schemas.search import SearchSourceType
    from specivo.services.embedding_service import EmbeddingService

    embedder = EmbeddingService()
    for entity_type, source_type in (
        (EntityType.ISSUE, SearchSourceType.ISSUE),
        (EntityType.WIKI_PAGE, SearchSourceType.WIKI_PAGE),
        (EntityType.JOURNAL, SearchSourceType.JOURNAL),
        (EntityType.ATTACHMENT, SearchSourceType.ATTACHMENT),
    ):
        await _index(ctx, embedder, entity_type, source_type)


async def _index(ctx: PhaseContext, embedder, entity_type: EntityType, source_type) -> None:
    """Chunk and embed the entities of one type this source has imported."""
    from specivo.services.search_index_service import describe_error

    entity_ids = sorted((await ctx.id_map.preload(ctx.session, entity_type)).values())
    for start in range(0, len(entity_ids), _INDEX_BATCH):
        for entity_id in entity_ids[start : start + _INDEX_BATCH]:
            try:
                await embedder.index_source(ctx.session, source_type, entity_id)
            except Exception as exc:  # noqa: BLE001 - see backfill_search_index
                ctx.warn(
                    "Could not index an item for search",
                    source=str(source_type),
                    id=entity_id,
                    reason=describe_error(exc),
                )
            ctx.tick()
        await ctx.session.flush()
