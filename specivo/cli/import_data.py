"""Import another tracker into Specivo.

One command for every import source. ``--source`` picks the adapter: the
built-in ``redmine``, or one a plugin listed in ``INSTALLED_PLUGINS`` adds
through ``get_import_sources()``. The adapter adds its own options (where its
database or export lives, mapping overrides); the run options below are the
same for every source.

Usage:
    python -m specivo.cli.import_data --list-sources
    python -m specivo.cli.import_data --source redmine --help

    # an import bundle written by an importer (docs/import-bundle/v1.md):
    # check it, rehearse it, then import it
    python -m specivo.cli.import_data --source bundle --path export.zip --validate
    python -m specivo.cli.import_data --source bundle --path export.zip --dry-run
    python -m specivo.cli.import_data --source bundle --path export.zip

    python -m specivo.cli.import_data --source redmine \\
        --source-db-url postgresql://redmine:secret@db.internal/redmine \\
        --source-files-dir /var/redmine/files \\
        --dry-run

    # or via the Makefile, which runs it inside the API container:
    make import-redmine ARGS='--source-db-url ... --source-files-dir ... --dry-run'

Start with --dry-run. It exercises the whole import and rolls it back, and its
report is the list of decisions worth reviewing before anything is written:
the project key chosen for each project, statuses whose category had to be
guessed, and anything that could not be carried across.

Running inside the container means the source has to be reachable from there:
its database over the network, its files or export mounted in. A read-only
mount is enough; nothing here writes to the source.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from specivo.importers.core.cli_args import parse_list, parse_map
from specivo.importers.core.pipeline import ImportOptions, ImportPhase, ImportPipeline
from specivo.importers.core.progress import ProgressReporter
from specivo.importers.core.source import SourceAdapter, SourceAdapterRegistry, UnknownSourceError

logger = logging.getLogger(__name__)

PROG = "python -m specivo.cli.import_data"


def _phase(value: str) -> ImportPhase:
    try:
        return ImportPhase(value)
    except ValueError:
        known = ", ".join(phase.value for phase in ImportPhase)
        raise argparse.ArgumentTypeError(f"unknown phase {value!r}; known phases: {known}") from None


def _run_id(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a run id (expected a UUID)") from None


def format_source_list(registry: SourceAdapterRegistry) -> str:
    """Return one line per registered source: name, origin and description."""
    entries = registry.entries()
    width = max((len(name) for name in entries), default=0)
    origin_width = max((len(entry.origin) for entry in entries.values()), default=0)
    lines = []
    for name in registry.names():
        entry = entries[name]
        lines.append(f"{name:<{width}}  {entry.origin:<{origin_width}}  {entry.description}".rstrip())
    return "\n".join(lines)


def build_parser(registry: SourceAdapterRegistry, adapter_cls: type[Any] | None = None) -> argparse.ArgumentParser:
    """Return the full parser: the generic options plus *adapter_cls*'s own.

    Without an adapter it is the parser ``--help`` shows before a source is
    chosen, listing the registered sources in its epilog.
    """
    sources = ", ".join(registry.names()) or "none"
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Import users, projects, issues, wiki pages and attachments from another tracker.",
        epilog=(
            f"Registered sources: {sources}. Use --source NAME --help for a source's own options. "
            "Run with --dry-run first: it exercises the whole import, rolls it back, and reports "
            "the decisions worth reviewing before anything is written."
        ),
    )
    parser.add_argument(
        "--source",
        metavar="NAME",
        required=True,
        help=f"The import source to read from ({sources}).",
    )
    parser.add_argument(
        "--list-sources",
        action="store_true",
        help="List the registered import sources and where each comes from, then exit.",
    )
    parser.add_argument(
        "--source-instance",
        help=(
            "Name for this installation of the source, used to scope the identifier map. "
            "Set it when importing two installations of the same system into one Specivo. "
            "Each source picks its own default."
        ),
    )

    scope = parser.add_argument_group("scope")
    scope.add_argument(
        "--project",
        type=parse_list,
        help="Comma-separated source project ids to import. Subprojects come with their parent. Default: all.",
    )

    mapping = parser.add_argument_group("mapping")
    mapping.add_argument(
        "--project-key-map",
        type=parse_map,
        help="Override derived project keys, as identifier=KEY pairs (e.g. acme-app=ACME,ops=OPS).",
    )
    mapping.add_argument(
        "--merge-duplicate-statuses",
        action="store_true",
        help="Reuse an existing status, tracker or priority of the same name instead of creating a second one.",
    )
    mapping.add_argument(
        "--import-passwords",
        action="store_true",
        help=(
            "Let imported accounts keep their old password until their first sign-in, which upgrades it. "
            "Requires the source's password algorithm in LEGACY_PASSWORD_ALGORITHMS. Administrators always "
            "set a new password. Without this flag every account does."
        ),
    )
    mapping.add_argument(
        "--flatten-excess-depth",
        action="store_true",
        help="Import projects nested deeper than Specivo allows by re-parenting them higher up.",
    )

    run = parser.add_argument_group("run")
    mode = run.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Exercise the whole import inside a transaction that is always rolled back.",
    )
    mode.add_argument(
        "--validate",
        action="store_true",
        help=(
            "Only check the source and report what is wrong with it; nothing is written and the database is not "
            "touched. For a bundle: schema, cross-reference and file checks. Exits 1 if the source is invalid."
        ),
    )
    run.add_argument(
        "--resume",
        metavar="RUN_ID",
        type=_run_id,
        help="Continue a previous run. Anything already imported is recognised and skipped either way.",
    )
    run.add_argument("--batch-size", type=int, default=500, help="Rows read per page from the source (default: 500).")
    run.add_argument(
        "--strict",
        action="store_true",
        help="Treat anything that would be reported as a warning as a failure. For testing an import, not running one.",
    )
    run.add_argument(
        "--report-json", metavar="PATH", help="Also write the summary (or the --validate report) to this file as JSON."
    )
    run.add_argument(
        "--stop-after-phase",
        metavar="PHASE",
        type=_phase,
        help="Stop once this phase completes. Used to test resuming.",
    )
    run.add_argument("--log-level", default="INFO", help="Logging level (default: INFO).")

    if adapter_cls is not None:
        adapter_cls.add_arguments(parser)
    return parser


def parse_args(argv: Sequence[str], registry: SourceAdapterRegistry) -> tuple[argparse.Namespace, type[Any]]:
    """Parse *argv* in two steps: find ``--source``, then parse with its options.

    The adapter's options are only known once the source is, so ``--source`` is
    read first, the adapter adds its arguments, and then the whole command line
    is parsed. Exits like argparse does for ``--help``, ``--list-sources``, a
    missing or unknown source and any other usage error.
    """
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--source")
    pre.add_argument("--list-sources", action="store_true")
    pre.add_argument("-h", "--help", action="store_true")
    known, _ = pre.parse_known_args(argv)

    if known.list_sources:
        print(format_source_list(registry))
        raise SystemExit(0)

    if known.source is None:
        generic = build_parser(registry)
        if known.help:
            generic.print_help()
            raise SystemExit(0)
        generic.error(f"--source is required. Registered sources: {', '.join(registry.names()) or 'none'}")

    try:
        adapter_cls = registry.get(known.source)
    except UnknownSourceError as exc:
        build_parser(registry).error(str(exc))

    args = build_parser(registry, adapter_cls).parse_args(argv)
    return args, adapter_cls


def build_options(args: argparse.Namespace, adapter: SourceAdapter) -> ImportOptions:
    """Translate the generic run options into :class:`ImportOptions`."""
    return ImportOptions(
        source_instance=adapter.source_instance,
        dry_run=args.dry_run,
        project_refs=args.project,
        resume_run_id=args.resume,
        stop_after_phase=args.stop_after_phase,
        batch_size=args.batch_size,
        strict=args.strict,
        project_key_map=args.project_key_map or {},
        flatten_excess_depth=args.flatten_excess_depth,
        merge_duplicate_statuses=args.merge_duplicate_statuses,
        import_passwords=args.import_passwords,
    )


def build_pipeline(
    args: argparse.Namespace,
    adapter: SourceAdapter,
    session_factory: async_sessionmaker[AsyncSession],
    reporter: ProgressReporter | None = None,
) -> ImportPipeline:
    """Return the import pipeline for *adapter*, with every loader registered."""
    from specivo.importers.load.registry import register_all

    return register_all(
        ImportPipeline(
            adapter=adapter,
            session_factory=session_factory,
            options=build_options(args, adapter),
            reporter=reporter,
        )
    )


async def run_validation(args: argparse.Namespace, adapter: Any) -> int:
    """Check the source without importing it, print the report, and return the exit code.

    Needs no database: only the source is read.
    """
    validate = getattr(adapter, "validate", None)
    if not callable(validate):
        logger.error("The %s source cannot be checked on its own; use --dry-run instead.", args.source)
        return 2

    report = await validate()
    print(report.format_text())
    if args.report_json:
        Path(args.report_json).write_text(json.dumps(report.as_dict(), indent=2))
        logger.info("Wrote the report to %s", args.report_json)
    return 0 if report.ok else 1


async def _run(args: argparse.Namespace, adapter_cls: type[Any]) -> int:
    from specivo.core.database import get_session_factory
    from specivo.importers.core.progress import CliProgressReporter

    adapter = adapter_cls.from_args(args)
    if args.validate:
        return await run_validation(args, adapter)
    pipeline = build_pipeline(args, adapter, get_session_factory(), CliProgressReporter())

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


def main(argv: Sequence[str] | None = None) -> None:
    from specivo.core.plugin_manager import bootstrap
    from specivo.importers.core.source import get_source_registry

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    argv = list(sys.argv[1:] if argv is None else argv)

    # Load plugins the way the web app does: a plugin can add an import source,
    # and the password hasher its credentials need, and both must be seen here.
    try:
        bootstrap()
    except Exception as exc:  # noqa: BLE001 - a misconfigured plugin is reported, not traced
        logger.error("Could not load plugins: %s", exc)
        sys.exit(1)

    args, adapter_cls = parse_args(argv, get_source_registry())
    logging.getLogger("specivo").setLevel(args.log_level.upper())

    try:
        sys.exit(asyncio.run(_run(args, adapter_cls)))
    except KeyboardInterrupt:
        # Phases commit as they finish, so an interrupted import is resumable.
        logger.error("Interrupted. Re-run with --resume and the run id printed above to continue.")
        sys.exit(130)
    except Exception as exc:  # pragma: no cover - CLI surface
        logger.error("Import failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
