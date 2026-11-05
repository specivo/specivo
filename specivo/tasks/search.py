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
  When they are held elsewhere a project run does nothing (a redelivered
  message, or a run dispatched while an instance run holds the scope), and an
  instance run waits for the project runs to finish.

A project manager must not be able to keep an instance reindex out by
starting project runs: an instance request is never refused because projects
are being reindexed. It takes the instance lock and, if project runs are
going, sets ``FTS_INSTANCE_PENDING_KEY``; from then on new project requests
are refused (atomically, see ``TokenLock.acquire_unless``) and the instance
task waits for the running ones to drain.
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
#: Set while an instance reindex waits for project reindexes to finish; refuses new project runs.
FTS_INSTANCE_PENDING_KEY = "fts:reindex:instance-pending"
#: Seconds an instance reindex waits for project reindexes before it gives up.
FTS_INSTANCE_WAIT_TIMEOUT = 1800
#: First and longest delay (seconds) between its checks; well under ``FTS_REINDEX_LOCK_TTL``, the
#: lifetime of the run lock and the pending marker, which every check extends.
FTS_INSTANCE_WAIT_FIRST_DELAY = 1.0
FTS_INSTANCE_WAIT_MAX_DELAY = 30.0


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

    def waiting() -> None:
        self.update_state(state="WAITING", meta={"waiting_for": "project reindexes"})

    return run_async(_locked_reindex(self.request.id, project_id, progress, waiting))


async def _locked_reindex(task_id: str | None, project_id: int | None, progress_cb, waiting_cb=None) -> dict:  # type: ignore[no-untyped-def]
    """Run the reindex under the scope's advisory locks, refreshing and finally releasing the Redis run lock.

    A project run that finds its scope held does nothing. An instance run
    waits instead, polling with a growing delay, for the project runs that
    were going when it was asked for: new ones cannot start meanwhile (the
    instance lock and the pending marker refuse them), so the wait ends. After
    ``FTS_INSTANCE_WAIT_TIMEOUT`` it gives up and records a failed result. The
    pending marker is cleared once the instance run holds its lock, and in any
    case when the task ends: on success, failure or timeout.
    """
    import asyncio
    import time

    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from specivo.core.config import get_settings
    from specivo.core.locks import LockLostError, TokenLock, try_advisory_locks
    from specivo.core.redis import get_redis

    redis = await get_redis()
    lock = TokenLock(redis, fts_lock_key(project_id))
    marker = TokenLock(redis, FTS_INSTANCE_PENDING_KEY)
    token = task_id or ""
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    deadline = time.monotonic() + FTS_INSTANCE_WAIT_TIMEOUT
    delay = FTS_INSTANCE_WAIT_FIRST_DELAY
    try:
        while True:
            async with try_advisory_locks(engine, fts_advisory_keys(project_id)) as acquired:
                if acquired:
                    if project_id is None:
                        await marker.release(token)

                    async def report(counts: dict[str, int]) -> None:
                        progress_cb(counts)
                        if not await _keep(lock, token):
                            # Another reindex holds our scope's run lock now: stop
                            # (the transaction rolls back) rather than run unguarded.
                            raise LockLostError(f"The run lock {lock.key} was taken over")

                    try:
                        return await _reindex_async(project_id, report)
                    except Exception as exc:
                        logger.exception("FTS reindex failed (project_id=%s)", project_id)
                        await _store_result(project_id, {"status": "failed", "error": str(exc)})
                        raise
                    finally:
                        await lock.release(token)
            if project_id is not None:
                # The scope (or the instance) is being reindexed: by this very
                # message redelivered, or by a run dispatched alongside. Its
                # Redis lock is not ours to touch.
                logger.info("FTS reindex of project %s skipped: another reindex holds the scope", project_id)
                return {"status": "skipped"}
            if time.monotonic() >= deadline:
                summary = {
                    "status": "failed",
                    "error": "Timed out waiting for the project reindexes in progress to finish",
                }
                logger.warning("FTS instance reindex gave up waiting for project reindexes")
                await _store_result(None, summary)
                await lock.release(token)
                return summary
            if waiting_cb is not None:
                waiting_cb()
            # Hold the instance lock and the pending marker on every pass, and
            # take them (again) when they expired, or were never set (a
            # redelivered message): without them project runs could start and
            # the wait might never end. Held by another reindex: stop.
            if not await _keep(lock, token) or not await _keep(marker, token):
                summary = {
                    "status": "failed",
                    "error": "Another instance reindex took over while this one waited for project reindexes",
                }
                logger.warning("FTS instance reindex %s stopped: its run lock or marker is held by another", token)
                await _store_result(None, summary)
                await lock.release(token)
                return summary
            await asyncio.sleep(delay)
            delay = min(delay * 2, FTS_INSTANCE_WAIT_MAX_DELAY)
    finally:
        if project_id is None:
            await marker.release(token)
        await engine.dispose()


async def _keep(lock, token: str) -> bool:  # type: ignore[no-untyped-def]
    """Hold *lock* for *token* (``TokenLock.hold``); False when another token holds it.

    While Redis cannot be asked nobody can take the lock either (dispatches
    fail closed), so the holder carries on and takes it again once Redis is
    back.
    """
    from specivo.core.locks import LockUnavailableError

    try:
        return bool(await lock.hold(token, FTS_REINDEX_LOCK_TTL))
    except LockUnavailableError:
        logger.warning("Could not reach Redis to hold %s; carrying on", lock.key)
        return True


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
