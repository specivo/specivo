"""Rebuild full-text-search vectors after an analyzer-language change.

Re-fires the per-table search-vector triggers so existing rows are re-tokenised
with the current (per-project or instance-default) FTS language. Safe to re-run.

Usage:
    python -m specivo.cli.reindex_fts                 # whole instance
    python -m specivo.cli.reindex_fts --project ACME  # one project
    # or via Makefile:
    make reindex-fts
    make reindex-fts PROJECT=ACME
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


async def _reindex(project_key: str | None) -> None:
    from specivo.core.database import get_session_factory
    from specivo.services.project_service import ProjectService
    from specivo.services.search_reindex_service import reindex_fts

    factory = get_session_factory()
    project_id: int | None = None

    async with factory() as session:
        if project_key:
            project = await ProjectService().get_by_key(session, project_key.upper())
            project_id = project.id
            logger.info("Reindexing FTS for project %s (id=%s)...", project.key, project_id)
        else:
            logger.info("Reindexing FTS for the whole instance...")

        async def _progress(counts: dict[str, int]) -> None:
            logger.info(
                "  issues=%s wiki=%s chunks=%s (total %s)",
                counts.get("issues", 0),
                counts.get("wiki_contents", 0),
                counts.get("search_chunks", 0),
                counts.get("total", 0),
            )

        start = time.monotonic()
        counts = await reindex_fts(session, project_id=project_id, progress_cb=None)
        await session.commit()
        await _progress(counts)
        logger.info("Done in %.1fs.", time.monotonic() - start)


def main() -> None:
    parser = argparse.ArgumentParser(description="Rebuild FTS search vectors.")
    parser.add_argument("--project", help="Project key to reindex (default: whole instance).")
    args = parser.parse_args()
    try:
        asyncio.run(_reindex(args.project))
    except Exception as exc:  # pragma: no cover - CLI surface
        logger.error("Reindex failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
