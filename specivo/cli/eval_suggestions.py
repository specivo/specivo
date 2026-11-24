"""Measure recall@k of suggested links against hybrid search, on labelled issues. Read-only.

The labelled file names issues and the wiki pages known to be right for each:

    # ISSUE-REF: page, page   (a page is wiki:KEY/slug, or a slug of the issue's project)
    ACME-12: wiki:ACME/runbook, deploy-guide
    ACME-31: architecture

or the same as JSON: ``{"ACME-12": ["wiki:ACME/runbook", "deploy-guide"]}``.

For every issue it ranks the issue's neighbours by document vector (chunk
mean, with and without the project's threshold) and by the best pair of
chunks, runs hybrid search with the issue's subject, and reports recall@k of
each (over issues and pages, and over pages only), plus the pages the
suggestions found that search missed. See ``specivo.services.suggestion_eval``.

It runs in a read-only database transaction as the user given with ``--as``
(default: the first active administrator), so it sees what that user sees,
and with the query embedding cache off, so it writes nothing to Redis either.

Exit status: 0 when every case was evaluated, 1 when some were skipped (an
unknown issue or page) or the run failed, 2 for bad arguments.

Usage:
    python -m specivo.cli.eval_suggestions labels.txt
    python -m specivo.cli.eval_suggestions labels.json --as alex --k 5 --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger("specivo.cli.eval_suggestions")


async def _main(labels: str, login: str | None, k: int, as_json: bool) -> int:
    from sqlalchemy import func, select, text

    from specivo.core.database import get_session_factory
    from specivo.models.user import User
    from specivo.services.suggestion_eval import evaluate, parse_labels

    cases = parse_labels(labels)
    if not cases:
        logger.error("No labelled issues in the file")
        return 2
    async with get_session_factory()() as session:
        # Nothing this command does may write: the database refuses it in this transaction.
        await session.execute(text("SET TRANSACTION READ ONLY"))
        if login:
            user = await session.scalar(select(User).where(func.lower(User.login) == login.lower()))
        else:
            user = await session.scalar(
                select(User).where(User.is_admin.is_(True), User.status == "active").order_by(User.id).limit(1)
            )
        if user is None:
            logger.error("No such user: %s", login or "(an active administrator)")
            await session.rollback()
            return 2
        report = await evaluate(session, user, cases, k=k)
        await session.rollback()
    print(json.dumps(report.as_dict(), indent=2, ensure_ascii=False) if as_json else report.as_text())
    return 1 if any(c.error for c in report.cases) else 0


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Recall@k of suggested links vs hybrid search (read-only).")
    parser.add_argument("labels", type=Path, help="Labelled issues: 'ISSUE-REF: page, page' lines, or JSON.")
    parser.add_argument("--as", dest="login", help="Run as this login (default: the first active administrator).")
    parser.add_argument("--k", type=int, default=5, help="Cut-off of the rankings (default 5).")
    parser.add_argument("--json", action="store_true", help="Print the full report as JSON.")
    args = parser.parse_args(argv)
    if args.k < 1 or args.k > 50:
        parser.error("--k must be between 1 and 50")
    if not args.labels.is_file():
        parser.error(f"No such file: {args.labels}")
    # The query embedding cache is the only thing search writes (to Redis): keep it off.
    os.environ["SEARCH_QUERY_EMBEDDING_CACHE_SECONDS"] = "0"
    from specivo.core.config import get_settings

    get_settings.cache_clear()
    try:
        sys.exit(asyncio.run(_main(args.labels.read_text(encoding="utf-8"), args.login, args.k, args.json)))
    except ValueError as exc:
        logger.error("Bad labels file: %s", exc)
        sys.exit(2)
    except Exception as exc:  # pragma: no cover - CLI surface  # noqa: BLE001
        logger.error("Evaluation failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
