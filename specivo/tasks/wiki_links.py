"""Celery tasks for wiki link graph rebuilding.

Tasks:
- rebuild_wiki_page_links: Parse wiki page content and rebuild the link graph.
"""

from __future__ import annotations

import logging

from specivo.core.constants import CELERY_MAX_RETRIES, CELERY_RETRY_DELAY_LINK_GRAPH
from specivo.tasks import celery_app
from specivo.tasks._async import run_async

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, max_retries=CELERY_MAX_RETRIES, default_retry_delay=CELERY_RETRY_DELAY_LINK_GRAPH)
def rebuild_wiki_page_links(self, wiki_id: int, page_id: int) -> None:  # type: ignore[no-untyped-def]
    """Rebuild the link graph for a single wiki page.

    Acquires a Redis lock per wiki to prevent concurrent rebuilds from
    corrupting the link table.

    Args:
        wiki_id: ID of the wiki containing the page.
        page_id: ID of the wiki page whose links should be rebuilt.
    """
    try:
        run_async(_rebuild_links_async(wiki_id, page_id))
    except Exception as exc:
        logger.warning("Failed to rebuild wiki links for page %d: %s", page_id, exc)
        raise self.retry(exc=exc)


async def _rebuild_links_async(wiki_id: int, page_id: int) -> None:
    """Async implementation of link graph rebuild with Redis lock."""
    import os

    import redis.asyncio as aioredis
    from redis.exceptions import LockNotOwnedError

    from specivo.services.wiki_link_service import WikiLinkService
    from specivo.tasks._async import task_session

    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    r = aioredis.from_url(redis_url)
    lock_key = f"specivo:wiki_link_graph:{wiki_id}"

    try:
        async with r.lock(lock_key, timeout=60, blocking_timeout=30):
            service = WikiLinkService()
            async with task_session() as session:
                # 1. Rebuild outgoing links for this page
                count = await service.rebuild_page_links(session, wiki_id, page_id)

                # 2. Resolve broken incoming links from other pages that
                #    reference this page's slug (e.g. page was just created)
                from specivo.models.wiki import WikiPage

                page = await session.get(WikiPage, page_id)
                if page:
                    await service.resolve_incoming_links(session, wiki_id, page_id, page.slug)

                await session.commit()
                logger.info("Rebuilt %d link(s) for wiki page %d", count, page_id)
    except LockNotOwnedError:
        # The lock's TTL expired before we released it (e.g. the worker was
        # starved under heavy contention). The rebuild above already committed,
        # so the work is done — only the lock cleanup failed. Swallow it rather
        # than retry. A plain LockError (failure to *acquire*) is deliberately
        # left to propagate so this on-demand task is retried instead of
        # silently leaving the page's links stale.
        logger.debug(
            "Wiki link rebuild lock lost mid-run for wiki %d page %d; links already rebuilt",
            wiki_id,
            page_id,
        )
    finally:
        await r.aclose()
