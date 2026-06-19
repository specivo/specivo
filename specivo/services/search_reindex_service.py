"""FTS reindex — rebuild stored search vectors after a language change.

Re-fires the per-table ``*_search_vector_update`` triggers (installed by
migration 0025) by touching the source column, in id-range batches so callers
can report live progress. Scoped to one project or the whole instance.

Used by the admin/project reindex Celery task and the ``reindex_fts`` CLI.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# (key, table, touch_col, project_scope_sql) — project_scope_sql restricts the
# batch to a single project when :project_id is provided.
_TARGETS = [
    ("issues", "issues", "subject", "project_id = :project_id"),
    (
        "wiki_contents",
        "wiki_contents",
        "text",
        "page_id IN (SELECT wp.id FROM wiki_pages wp JOIN wikis w ON w.id = wp.wiki_id "
        "WHERE w.project_id = :project_id)",
    ),
    (
        "search_chunks",
        "search_chunks",
        "content",
        "source_id IN (SELECT id FROM search_sources WHERE project_id = :project_id)",
    ),
]

ProgressCb = Callable[[dict[str, int]], Awaitable[None] | None]


async def reindex_fts(
    session: AsyncSession,
    *,
    project_id: int | None = None,
    batch_size: int = 500,
    progress_cb: ProgressCb | None = None,
) -> dict[str, int]:
    """Rebuild ``search_vector`` for issues, wiki_contents and search_chunks.

    Touches the trigger source column in ascending-id batches. When
    ``project_id`` is given, only that project's rows are rebuilt. Returns a
    dict of per-target row counts (plus ``total``). ``progress_cb`` is invoked
    with the running counts after each batch.
    """
    counts: dict[str, int] = {key: 0 for key, _, _, _ in _TARGETS}

    async def _emit() -> None:
        if progress_cb is not None:
            counts["total"] = sum(v for k, v in counts.items() if k != "total")
            result = progress_cb(dict(counts))
            if result is not None:
                await result

    for key, table, touch_col, scope_sql in _TARGETS:
        where = scope_sql if project_id is not None else "TRUE"
        last_id = 0
        while True:
            stmt = text(
                f"""
                UPDATE {table} SET {touch_col} = {touch_col}
                WHERE id IN (
                    SELECT id FROM {table}
                    WHERE id > :last_id AND ({where})
                    ORDER BY id
                    LIMIT :batch_size
                )
                RETURNING id
                """
            )
            params: dict[str, object] = {"last_id": last_id, "batch_size": batch_size}
            if project_id is not None:
                params["project_id"] = project_id
            rows = (await session.execute(stmt, params)).scalars().all()
            if not rows:
                break
            last_id = max(rows)
            counts[key] += len(rows)
            await session.flush()
            await _emit()

    counts["total"] = sum(v for k, v in counts.items() if k != "total")
    logger.info("FTS reindex complete (project_id=%s): %s", project_id, counts)
    return counts
