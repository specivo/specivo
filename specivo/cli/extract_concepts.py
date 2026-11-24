"""Run concept extraction for one project now, instead of waiting for the nightly sweep.

Sends the project's changed issues and wiki pages (only those whose content
changed since they were last extracted) to the project's own AI provider, as
the ``extract_concepts`` task does, and prints what the run did. The project
must have concept extraction switched on and a provider set.

Exit status: 0 when the run finished, 1 when it could not run or stopped on
provider errors.

Usage:
    python -m specivo.cli.extract_concepts --project ACME
    python -m specivo.cli.extract_concepts --project ACME --limit 10   # at most 10 sources this time
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

logger = logging.getLogger("specivo.cli.extract_concepts")


async def _main(project_key: str, limit: int | None) -> int:
    from specivo.core.database import get_session_factory
    from specivo.core.locks import beat_lock
    from specivo.services.concept_extraction import STATUS_DONE, extract_project
    from specivo.services.project_service import ProjectService
    from specivo.tasks.concepts import run_lock_key

    factory = get_session_factory()
    async with factory() as session:
        project = await ProjectService().get_by_key(session, project_key.upper())
        project_id = project.id
    async with beat_lock(run_lock_key(project_id), 6 * 3600) as acquired:
        if not acquired:
            logger.error("Concept extraction of %s is already running.", project_key.upper())
            return 1
        async with factory() as session:
            result = await extract_project(session, project_id, limit=limit)
    print(json.dumps(result.as_dict(), indent=1))
    for error in result.errors[:5]:
        logger.warning("Provider error: %s", error)
    return 0 if result.status == STATUS_DONE else 1


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Extract concepts of a project's changed issues and pages now.")
    parser.add_argument("--project", required=True, help="Project key.")
    parser.add_argument("--limit", type=int, default=None, help="Sources to send at most (default: the project's).")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    try:
        sys.exit(asyncio.run(_main(args.project, args.limit)))
    except Exception as exc:  # pragma: no cover - CLI surface  # noqa: BLE001
        logger.error("Concept extraction failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
