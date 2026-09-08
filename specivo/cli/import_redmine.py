"""Import a Redmine instance into Specivo.

Reads Redmine's database directly and copies its attachment files, rather than
going through its REST API: an import needs history, private notes and every
user, and reading the database is both complete and far faster. It assumes the
operator has that access, which is the position somebody migrating their own
tracker is in.

Usage:
    python -m specivo.cli.import_redmine \\
        --source-db-url postgresql://redmine:secret@db.internal/redmine \\
        --source-files-dir /var/redmine/files \\
        --dry-run

    # or via the Makefile, which runs it inside the API container:
    make import-redmine ARGS='--source-db-url ... --source-files-dir ... --dry-run'

Start with --dry-run. It exercises the whole import and rolls it back, and its
report is the list of decisions worth reviewing before anything is written:
the project key chosen for each project, statuses whose category had to be
guessed, and anything that could not be carried across.

Running inside the container means the Redmine database has to be reachable
from there, and its files directory mounted into it. A read-only mount is
enough; nothing here writes to the source.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import uuid
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


def _parse_map(value: str | None) -> dict[str, str]:
    """Parse ``a=b,c=d`` into a dict, tolerating spaces around the parts."""
    if not value:
        return {}
    pairs: dict[str, str] = {}
    for item in value.split(","):
        if not item.strip():
            continue
        key, separator, mapped = item.partition("=")
        if not separator:
            raise argparse.ArgumentTypeError(f"Expected key=value, got {item.strip()!r}")
        pairs[key.strip()] = mapped.strip()
    return pairs


def _parse_list(value: str | None) -> tuple[str, ...] | None:
    """Parse a comma-separated list, returning None when empty."""
    if not value:
        return None
    items = tuple(item.strip() for item in value.split(",") if item.strip())
    return items or None


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser, kept separate so it can be tested."""
    parser = argparse.ArgumentParser(
        prog="python -m specivo.cli.import_redmine",
        description="Import projects, issues, wiki pages and attachments from a Redmine instance.",
        epilog=(
            "Run with --dry-run first: it exercises the whole import, rolls it back, and reports "
            "the decisions worth reviewing before anything is written."
        ),
    )
    source = parser.add_argument_group("source")
    source.add_argument(
        "--source-db-url",
        required=True,
        help="Redmine database URL. MySQL and PostgreSQL are both supported, with or without an async driver.",
    )
    source.add_argument(
        "--source-files-dir",
        help="Redmine's files directory. Without it, attachments are skipped.",
    )
    source.add_argument(
        "--source-instance",
        help=(
            "Name for this Redmine installation, used to scope the identifier map. "
            "Set it when importing two installations into one Specivo. Defaults to the database host."
        ),
    )

    scope = parser.add_argument_group("scope")
    scope.add_argument(
        "--project",
        help="Comma-separated Redmine project ids to import. Subprojects come with their parent. Default: all.",
    )

    mapping = parser.add_argument_group("mapping")
    mapping.add_argument(
        "--project-key-map",
        help="Override derived project keys, as identifier=KEY pairs (e.g. acme-app=ACME,ops=OPS).",
    )
    mapping.add_argument(
        "--status-category-map",
        help=(
            "Place a status in Specivo's backlog/active/done/closed grouping, as name=category pairs. "
            "Redmine records only whether a status closes an issue, so the rest is guessed."
        ),
    )
    mapping.add_argument(
        "--cf-key-map",
        help="Override the metadata key derived from a custom field name, as name=key pairs.",
    )
    mapping.add_argument(
        "--merge-duplicate-statuses",
        action="store_true",
        help="Reuse an existing status, tracker or priority of the same name instead of creating a second one.",
    )
    mapping.add_argument(
        "--flatten-excess-depth",
        action="store_true",
        help="Import projects nested deeper than Specivo allows by re-parenting them higher up.",
    )

    run = parser.add_argument_group("run")
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="Exercise the whole import inside a transaction that is always rolled back.",
    )
    run.add_argument(
        "--resume",
        metavar="RUN_ID",
        help="Continue a previous run. Anything already imported is recognised and skipped either way.",
    )
    run.add_argument("--batch-size", type=int, default=500, help="Rows read per page from the source (default: 500).")
    run.add_argument(
        "--strict",
        action="store_true",
        help="Treat anything that would be reported as a warning as a failure. For testing an import, not running one.",
    )
    run.add_argument("--report-json", metavar="PATH", help="Also write the summary to this file as JSON.")
    run.add_argument(
        "--stop-after-phase",
        metavar="PHASE",
        help="Stop once this phase completes. Used to test resuming.",
    )
    run.add_argument("--log-level", default="INFO", help="Logging level (default: INFO).")
    return parser


async def _run(args: argparse.Namespace) -> int:
    from specivo.core.database import get_session_factory
    from specivo.importers.core.pipeline import ImportOptions, ImportPhase, ImportPipeline
    from specivo.importers.core.progress import CliProgressReporter
    from specivo.importers.load.registry import register_all
    from specivo.importers.redmine import RedmineSourceAdapter

    stop_after = None
    if args.stop_after_phase:
        try:
            stop_after = ImportPhase(args.stop_after_phase)
        except ValueError:
            known = ", ".join(phase.value for phase in ImportPhase)
            logger.error("Unknown phase %r. Known phases: %s", args.stop_after_phase, known)
            return 2

    adapter = RedmineSourceAdapter(
        source_db_url=args.source_db_url,
        source_files_dir=args.source_files_dir,
        source_instance=args.source_instance,
        status_category_overrides=_parse_map(args.status_category_map),
        cf_key_overrides=_parse_map(args.cf_key_map),
        batch_size=args.batch_size,
    )

    options = ImportOptions(
        source_instance=adapter.source_instance,
        dry_run=args.dry_run,
        project_refs=_parse_list(args.project),
        resume_run_id=uuid.UUID(args.resume) if args.resume else None,
        stop_after_phase=stop_after,
        batch_size=args.batch_size,
        strict=args.strict,
        project_key_map=_parse_map(args.project_key_map),
        status_category_map=_parse_map(args.status_category_map),
        cf_key_map=_parse_map(args.cf_key_map),
        flatten_excess_depth=args.flatten_excess_depth,
        merge_duplicate_statuses=args.merge_duplicate_statuses,
    )

    if args.source_files_dir is None:
        logger.warning("No --source-files-dir given: attachments will be skipped.")

    pipeline = register_all(
        ImportPipeline(
            adapter=adapter,
            session_factory=get_session_factory(),
            options=options,
            reporter=CliProgressReporter(),
        )
    )

    summary = await pipeline.run()

    print(summary.format_text())
    if args.report_json:
        Path(args.report_json).write_text(json.dumps(summary.as_dict(), indent=2))
        logger.info("Wrote the report to %s", args.report_json)

    if summary.dry_run:
        logger.info("Dry run complete. Nothing was written. Re-run without --dry-run to import.")
    else:
        logger.info("Import complete. Run id %s — pass it to --resume to continue this import.", summary.run_id)
    return 0


def main() -> None:
    args = build_parser().parse_args()
    logging.getLogger("specivo").setLevel(args.log_level.upper())

    try:
        sys.exit(asyncio.run(_run(args)))
    except KeyboardInterrupt:
        # Phases commit as they finish, so an interrupted import is resumable.
        logger.error("Interrupted. Re-run with --resume and the run id printed above to continue.")
        sys.exit(130)
    except Exception as exc:  # pragma: no cover - CLI surface
        logger.error("Import failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
