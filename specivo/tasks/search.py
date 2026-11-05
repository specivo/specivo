"""Celery task for FTS reindexing with live progress.

Rebuilds stored search vectors (after an analyzer-language change), scoped to a
single project or the whole instance. Reports progress via Celery
``update_state`` and persists a last-run summary into settings so the admin UI
can show it after the job finishes.

One run at a time per scope, and never an instance run alongside a project run
(both rewrite the same rows):

* the request takes a Redis run lock holding the task id (``fts_lock_key``,
  ``SET NX EX``; see ``search_admin._dispatch_reindex``), which the task
  refreshes as it reports progress and releases (compare-and-delete) at the end,
  on success and on failure;
* the task takes advisory locks in the ``FTS_REINDEX_LOCK_NAMESPACE``: an
  instance run key 0 exclusively, a project run key 0 *shared* plus its own
  project id exclusively. So project runs of different projects proceed side by
  side, and an instance run excludes them all and is excluded by any of them.
  When the locks are held elsewhere (a redelivered message, or a run of the
  other scope dispatched in the same instant) the task does nothing.
"""

from __future__ import annotations

import json
import logging

from specivo.tasks import celery_app
from specivo.tasks._async import run_async, task_session

logger = logging.getLogger(__name__)


def last_result_key(project_id: int | None) -> str:
    """Settings key under which the last reindex summary is stored."""
    return "fts_reindex_last_result" if project_id is None else f"fts_reindex_last_result:project:{project_id}"


def running_task_key(project_id: int | None) -> str:
    """Settings key under which the in-flight reindex task id is stored."""
    return "fts_reindex_task_id" if project_id is None else f"fts_reindex_task_id:project:{project_id}"


#: First key of the advisory locks a reindex takes (distinct from the link graph rebuild's).
FTS_REINDEX_LOCK_NAMESPACE = 0x5E51
#: Seconds the Redis run lock lives without a refresh; refreshed at each progress report.
FTS_REINDEX_LOCK_TTL = 300


def fts_lock_key(project_id: int | None) -> str:
    """Redis key of the run lock of a scope; ``FTS_PROJECT_LOCK_PATTERN`` matches the project ones."""
    return "fts:reindex:all" if project_id is None else f"fts:reindex:project:{project_id}"


FTS_PROJECT_LOCK_PATTERN = "fts:reindex:project:*"


def fts_advisory_keys(project_id: int | None) -> list[tuple[int, int, bool]]:
    """The advisory locks a reindex of the scope holds: see the module docstring."""
    if project_id is None:
        return [(FTS_REINDEX_LOCK_NAMESPACE, 0, False)]
    return [(FTS_REINDEX_LOCK_NAMESPACE, 0, True), (FTS_REINDEX_LOCK_NAMESPACE, project_id, False)]


def reindex_needed_key(project_id: int | None) -> str:
    """Settings key flagging that a language changed but rows aren't rebuilt yet."""
    return "fts_reindex_needed" if project_id is None else f"fts_reindex_needed:project:{project_id}"


@celery_app.task(bind=True)
def reindex_fts_task(self, project_id: int | None = None):  # type: ignore[no-untyped-def]
    """Rebuild FTS vectors for one project (or all). Returns final counts, or ``{"status": "skipped"}``."""

    def progress(counts: dict[str, int]) -> None:
        self.update_state(state="PROGRESS", meta=counts)

    return run_async(_locked_reindex(self.request.id, project_id, progress))


async def _locked_reindex(task_id: str | None, project_id: int | None, progress_cb) -> dict:  # type: ignore[no-untyped-def]
    """Run the reindex under the scope's advisory locks, refreshing and finally releasing the Redis run lock."""
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from specivo.core.config import get_settings
    from specivo.core.locks import TokenLock, try_advisory_locks
    from specivo.core.redis import get_redis

    lock = TokenLock(await get_redis(), fts_lock_key(project_id))
    token = task_id or ""
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        async with try_advisory_locks(engine, fts_advisory_keys(project_id)) as acquired:
            if not acquired:
                # The scope (or the other scope) is being reindexed: by this
                # very message redelivered, or by a run dispatched alongside.
                # Its Redis lock is not ours to touch.
                logger.info("FTS reindex of project %s skipped: another reindex holds the scope", project_id)
                return {"status": "skipped"}

            async def report(counts: dict[str, int]) -> None:
                progress_cb(counts)
                await lock.refresh(token, FTS_REINDEX_LOCK_TTL)

            try:
                return await _reindex_async(project_id, report)
            except Exception as exc:
                logger.exception("FTS reindex failed (project_id=%s)", project_id)
                await _store_result(project_id, {"status": "failed", "error": str(exc)})
                raise
            finally:
                await lock.release(token)
    finally:
        await engine.dispose()


async def _reindex_async(project_id: int | None, progress_cb) -> dict:  # type: ignore[no-untyped-def]
    from specivo.core.utils import utcnow
    from specivo.services.search_reindex_service import reindex_fts

    async with task_session() as session:
        counts = await reindex_fts(session, project_id=project_id, progress_cb=progress_cb)
        await session.commit()

    summary = {"status": "success", "counts": counts, "finished_at": utcnow().isoformat()}
    await _store_result(project_id, summary)
    return summary


async def _store_result(project_id: int | None, summary: dict) -> None:
    from specivo.services.settings_service import SettingsService

    updates = {last_result_key(project_id): json.dumps(summary)}
    if summary.get("status") == "success":
        updates[reindex_needed_key(project_id)] = "0"
    async with task_session() as session:
        await SettingsService().set_many(session, updates)
        await session.commit()
