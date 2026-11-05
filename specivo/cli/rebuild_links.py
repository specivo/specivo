"""Rebuild the entity link graph: the links written in every issue, comment and wiki page.

Each source's stored links are replaced with the links its text holds now, and
links written before their target existed are resolved. Run it once after
upgrading to a release that adds link kinds (the migration only carries over
wiki-to-wiki links), or whenever the graph is suspected stale, for example after
queued rebuilds were lost with a dead broker. Safe to re-run: the result
depends only on the text. Work is committed in batches, so an interrupted run
loses little.

Exit status: 0 on success, 1 when some sources could not be rebuilt or the run failed.

Usage:
    python -m specivo.cli.rebuild_links                 # whole instance
    python -m specivo.cli.rebuild_links --project ACME  # one project
    # or via Makefile:
    make rebuild-links
    make rebuild-links PROJECT=ACME
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

logger = logging.getLogger("specivo.cli.rebuild_links")


async def _main(project_key: str | None) -> int:
    from specivo.core.database import get_engine, get_session_factory
    from specivo.core.locks import try_advisory_lock
    from specivo.services.link_graph_admin_service import LINK_REBUILD_LOCK_NAMESPACE, run_full_rebuild
    from specivo.services.project_service import ProjectService

    factory = get_session_factory()
    project_id: int | None = None
    if project_key:
        async with factory() as session:
            project_id = (await ProjectService().get_by_key(session, project_key.upper())).id
    logger.info("Rebuilding links for %s...", f"project {project_key.upper()}" if project_key else "the whole instance")

    async def progress(processed: int, total: int) -> None:
        logger.info("  %d / %d sources", processed, total)

    # The lock a rebuild started from project settings takes: one run per scope.
    async with try_advisory_lock(get_engine(), LINK_REBUILD_LOCK_NAMESPACE, project_id or 0) as acquired:
        if not acquired:
            logger.error("A link graph rebuild is already running for this scope.")
            return 1
        summary = await run_full_rebuild(factory, project_id, triggered_by="command line", progress=progress)
    for error in summary["errors"]:
        logger.error("  %s", error)
    logger.info(
        "Done: %d sources, %d failed, %d links (%d unresolved), in %.1fs.",
        summary["sources"],
        summary["failed"],
        summary["edges"],
        summary["unresolved"],
        summary["duration_seconds"],
    )
    return 1 if summary["failed"] else 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Rebuild the links stored for issues, comments and wiki pages.")
    parser.add_argument("--project", help="Project key to rebuild (default: whole instance).")
    args = parser.parse_args()
    try:
        sys.exit(asyncio.run(_main(args.project)))
    except Exception as exc:  # pragma: no cover - CLI surface
        logger.error("Rebuilding links failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
