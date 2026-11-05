"""Celery task that keeps the entity link graph in step with content.

Tasks:
- rebuild_entity_links: rebuild the stored links of the given sources.
"""

from __future__ import annotations

import logging

from specivo.core.constants import CELERY_MAX_RETRIES, CELERY_RETRY_DELAY_LINK_GRAPH
from specivo.tasks import celery_app
from specivo.tasks._async import run_async

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, max_retries=CELERY_MAX_RETRIES, default_retry_delay=CELERY_RETRY_DELAY_LINK_GRAPH)
def rebuild_entity_links(self, sources: list[list]) -> None:  # type: ignore[no-untyped-def]
    """Rebuild the stored links of each ``[source_type, id]`` in *sources* from its committed text.

    Queued by ``entity_link_index`` after a commit. Idempotent, so a retry or
    a duplicate message only redoes the same work. Each source is rebuilt in
    a transaction of its own: one that fails is logged and retried with the
    task, without undoing the others.
    """
    keys = [(str(source_type), int(source_id)) for source_type, source_id in sources]
    try:
        failed = run_async(_rebuild_async(keys))
    except Exception as exc:
        logger.warning("Failed to rebuild links of %d source(s): %s", len(keys), exc)
        raise self.retry(exc=exc) from exc
    if failed:
        raise self.retry(
            args=[[list(key) for key in failed]],
            exc=RuntimeError(f"Could not rebuild links of {len(failed)} source(s)"),
        )


async def _rebuild_async(keys: list[tuple[str, int]]) -> list[tuple[str, int]]:
    from specivo.services.entity_link_service import EntityLinkService
    from specivo.tasks._async import task_session

    service = EntityLinkService()
    async with task_session() as session:
        sources = await service.expand_keys(session, keys)
        failures = await service.rebuild_sources(session, sources, commit_each=True)
    logger.info("Rebuilt links of %d source(s), %d failed", len(sources), len(failures))
    return [key for key, _reason in failures]


@celery_app.task(bind=True)
def rebuild_link_graph_task(self, project_id: int | None = None, triggered_by: str | None = None):  # type: ignore[no-untyped-def]
    """Rebuild every stored link of a project (or the instance), reporting progress.

    Dispatched by the settings page after taking the Redis run lock under this
    task's id; see ``link_graph_admin_service`` for the locking. Returns the
    summary it stores as the last result, or ``{"status": "skipped"}`` when
    another run (or an import) holds the project's advisory lock.
    """

    async def progress(processed: int, total: int) -> None:
        self.update_state(state="PROGRESS", meta={"processed": processed, "total": total})

    return run_async(_full_rebuild_async(self.request.id, project_id, triggered_by, progress))


async def _full_rebuild_async(task_id: str | None, project_id: int | None, triggered_by: str | None, progress) -> dict:  # type: ignore[no-untyped-def]
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from specivo.core.config import get_settings
    from specivo.core.locks import LockLostError, LockUnavailableError, TokenLock, try_advisory_lock
    from specivo.core.redis import get_redis
    from specivo.core.utils import utcnow
    from specivo.services.link_graph_admin_service import (
        LINK_REBUILD_LOCK_NAMESPACE,
        LINK_REBUILD_LOCK_TTL,
        rebuild_lock_key,
        run_full_rebuild,
        store_last_result,
    )

    lock = TokenLock(await get_redis(), rebuild_lock_key(project_id))
    token = task_id or ""
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with try_advisory_lock(engine, LINK_REBUILD_LOCK_NAMESPACE, project_id or 0) as acquired:
            if not acquired:
                # A redelivered message (same id, so the same Redis token) or
                # an import holds the scope: leave its lock alone.
                logger.info("Link graph rebuild of project %s already running; nothing to do", project_id)
                return {"status": "skipped"}

            async def report(processed: int, total: int) -> None:
                await progress(processed, total)
                try:
                    held = await lock.hold(token, LINK_REBUILD_LOCK_TTL)
                except LockUnavailableError:
                    # Nobody can take it while Redis is away (dispatch fails
                    # closed); it is taken again on the next batch.
                    logger.warning("Could not reach Redis to hold %s; carrying on", lock.key)
                    held = True
                if not held:
                    # Another rebuild holds this scope's run lock now: stop
                    # rather than carry on unguarded (each source is committed
                    # on its own, so what was rebuilt stays rebuilt).
                    raise LockLostError(f"The run lock {lock.key} was taken over")

            try:
                return await run_full_rebuild(factory, project_id, triggered_by=triggered_by, progress=report)
            except Exception as exc:
                logger.exception("Link graph rebuild failed (project_id=%s)", project_id)
                async with factory() as session:
                    await store_last_result(
                        session,
                        project_id,
                        {
                            "status": "failed",
                            "error": str(exc)[:500],
                            "finished_at": utcnow().isoformat(),
                            "triggered_by": triggered_by,
                        },
                    )
                    await session.commit()
                raise
            finally:
                await lock.release(token)
    finally:
        await engine.dispose()
