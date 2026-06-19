"""Celery task for FTS reindexing with live progress.

Rebuilds stored search vectors (after an analyzer-language change), scoped to a
single project or the whole instance. Reports progress via Celery
``update_state`` and persists a last-run summary into settings so the admin UI
can show it after the job finishes.
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


def reindex_needed_key(project_id: int | None) -> str:
    """Settings key flagging that a language changed but rows aren't rebuilt yet."""
    return "fts_reindex_needed" if project_id is None else f"fts_reindex_needed:project:{project_id}"


@celery_app.task(bind=True)
def reindex_fts_task(self, project_id: int | None = None):  # type: ignore[no-untyped-def]
    """Rebuild FTS vectors for one project (or all). Returns final counts."""

    def progress(counts: dict[str, int]) -> None:
        self.update_state(state="PROGRESS", meta=counts)

    try:
        return run_async(_reindex_async(project_id, progress))
    except Exception as exc:
        logger.exception("FTS reindex failed (project_id=%s)", project_id)
        run_async(_store_result(project_id, {"status": "failed", "error": str(exc)}))
        raise


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
