"""Compute suggested links (``similar`` edges) now, instead of waiting for the nightly sweep.

For each project (or the one given) it recomputes the document vectors of its
issues and wiki pages from their chunk embeddings, then stores each item's
nearest neighbours as open suggestions, as ``compute_similar_links`` does.
Accepted and dismissed pairs are kept. Each project is committed on its own.

Exit status: 0 on success, 1 when a project failed or there is no default embedding model.

Usage:
    python -m specivo.cli.similar_links                 # every project with embeddings
    python -m specivo.cli.similar_links --project ACME  # one project
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

logger = logging.getLogger("specivo.cli.similar_links")


async def _main(project_key: str | None) -> int:
    from specivo.core.database import get_session_factory
    from specivo.services.link_suggestion_service import (
        compute_for_project,
        default_model_id,
        projects_with_vectors_or_embeddings,
    )
    from specivo.services.project_service import ProjectService

    factory = get_session_factory()
    async with factory() as session:
        model_id = await default_model_id(session)
        if model_id is None:
            logger.error("No default embedding model: nothing to suggest from.")
            return 1
        if project_key:
            project_ids = [(await ProjectService().get_by_key(session, project_key.upper())).id]
        else:
            project_ids = await projects_with_vectors_or_embeddings(session, model_id)
    failed = 0
    for project_id in project_ids:
        async with factory() as session:
            try:
                stored = await compute_for_project(session, project_id, model_id)
                await session.commit()
            except Exception as exc:  # one project must not stop the others  # noqa: BLE001
                await session.rollback()
                logger.error("Project %d failed: %s", project_id, exc)
                failed += 1
                continue
        logger.info("Project %d: %d suggested link(s)", project_id, stored)
    return 1 if failed else 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Compute suggested links from the embeddings now.")
    parser.add_argument("--project", help="Project key (default: every project with embeddings).")
    args = parser.parse_args()
    try:
        sys.exit(asyncio.run(_main(args.project)))
    except Exception as exc:  # pragma: no cover - CLI surface  # noqa: BLE001
        logger.error("Computing suggested links failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
