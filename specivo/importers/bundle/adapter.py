"""The ``bundle`` source: import from an import bundle on disk.

A bundle is what an importer outside Specivo writes (see
``docs/import-bundle/v1.md``), so this adapter is the one way in for every
system that has an importer, Specivo knowing nothing about any of them.

``connect`` opens the bundle (unpacking a zip into a temporary directory),
then checks all of it before the first phase runs: an import never starts on a
bundle that fails validation. The same check is what ``--validate`` prints.

Records are read back from the files by byte offset, one at a time, in the
order the scan worked out, so no JSON-lines file is ever held whole in memory.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import AsyncIterator, Iterable, Iterator
from pathlib import Path
from typing import Any, ClassVar, TypeVar
from zipfile import BadZipFile, ZipFile

from specivo.importers.bundle import format as bundle_format
from specivo.importers.bundle.codec import LOOKUP_TYPES, from_record
from specivo.importers.bundle.container import BundleContainer, open_bundle, open_regular
from specivo.importers.bundle.format import FILES_DIR, LOOKUP_LISTS, MANIFEST, SHA256_RE, RecordFile
from specivo.importers.bundle.limits import BundleLimits, format_size, parse_size, positive_int
from specivo.importers.bundle.report import ProblemCode, ValidationReport
from specivo.importers.bundle.scan import BundleIndex, iter_lines, loads, scan_bundle
from specivo.importers.core.ir import (
    IRAttachment,
    IRCategory,
    IRCustomField,
    IRGroup,
    IRIssue,
    IRJournalEntry,
    IRLookups,
    IRMembership,
    IRProject,
    IRPublicAccess,
    IRRelation,
    IRSprint,
    IRTimeEntry,
    IRUser,
    IRVersion,
    IRWatcher,
    IRWikiPage,
    IRWikiRedirect,
)
from specivo.importers.core.links_file import default_links_path
from specivo.importers.core.source import BaseSourceAdapter

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Report sections.
NOTE_NOT_CARRIED_OVER = "left_behind_by_the_exporter"
NOTE_EXTRA = "records_with_source_data_kept_only_in_the_bundle"
NOTE_VALIDATION_WARNINGS = "bundle_validation_warnings"

# How many problems an import that refuses a bundle puts in its error.
_ERRORS_SHOWN = 20


class BundleInvalidError(ValueError):
    """The bundle failed validation, so nothing was imported."""

    def __init__(self, report: ValidationReport) -> None:
        self.report = report
        super().__init__(f"The bundle is not valid; nothing was imported.\n{report.format_text(limit=_ERRORS_SHOWN)}")


class BundleSourceAdapter(BaseSourceAdapter):
    """Reads an import bundle: a directory, or a ``.zip`` of one.

    ``bundle`` names the source on the command line only. The identifier map
    is keyed by the system and instance the manifest names: two bundles from
    the same installation share one map, which is what lets a later bundle add
    to an earlier import.
    """

    source_system: ClassVar[str] = "bundle"
    description: ClassVar[str] = "An import bundle (directory or .zip) written by an importer"
    # The identifier-map scope comes from the manifest, not the operator, so
    # adding to an earlier import of it needs --continue-import.
    scope_from_source_data: ClassVar[bool] = True

    def __init__(
        self,
        path: str | Path,
        source_instance: str | None = None,
        limits: BundleLimits | None = None,
        files_path: str | Path | None = None,
        links_file: str | Path | None = None,
    ) -> None:
        self.path = Path(path)
        self.limits = limits or BundleLimits()
        # Stored files from a directory of their own instead of the bundle's files/.
        self.files_path = Path(files_path) if files_path is not None else None
        # Where text waiting for a later bundle's records is kept (see links_path).
        self.links_file = Path(links_file) if links_file is not None else None
        system, instance = _source_from_manifest(self.path, self.limits)
        self._id_map_system = system
        super().__init__(source_instance or instance)
        self._container: BundleContainer | None = None
        self._index: BundleIndex | None = None
        self.report: ValidationReport | None = None

    @property
    def id_map_system(self) -> str:
        """The system the manifest names, which the identifier map is keyed by."""
        return self._id_map_system

    @property
    def links_path(self) -> Path:
        """The links file: ``--links-file``, or one named after the scope next to the bundle.

        See :mod:`specivo.importers.load.pending_links`.
        """
        if self.links_file is not None:
            return self.links_file
        return default_links_path(self.path.absolute().parent, self.id_map_system, self.source_instance)

    # ------------------------------------------------------------------
    # Command line
    # ------------------------------------------------------------------

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser) -> None:
        group = parser.add_argument_group("bundle source")
        group.add_argument(
            "--path",
            required=True,
            help="The bundle: a directory, or a .zip archive of one (unpacked to a temporary directory).",
        )
        group.add_argument(
            "--files-path",
            metavar="DIR",
            help=(
                "Read the stored attachment files (files/<sha256>) from this directory instead of the bundle's own "
                "files/, which is then ignored. For large migrations: copy the files over on their own (rsync "
                "resumes and skips what is there) and keep the bundle small. Works with a zip too."
            ),
        )
        group.add_argument(
            "--links-file",
            metavar="PATH",
            help=(
                "The links file: a small SQLite file remembering text this import writes as plain text because it "
                "names records not imported yet, so a later bundle of the same source can turn it into links. "
                "Default: specivo-links.<system>.<instance>.sqlite next to the bundle. Give the same file to every "
                "import of the source, on a mounted folder when imports run in Docker. A throwaway: delete it "
                "after cut-over, and those references stay plain text."
            ),
        )
        limits = parser.add_argument_group(
            "bundle limits",
            "A bundle is untrusted input. Raise these only for a bundle you know to be that large.",
        )
        default = BundleLimits()
        limits.add_argument(
            "--max-unpacked-size",
            type=parse_size,
            default=default.max_unpacked_bytes,
            metavar="SIZE",
            help=(
                f"Most a zip may unpack to, counted as it unpacks (default: {format_size(default.max_unpacked_bytes)})."
            ),
        )
        limits.add_argument(
            "--max-entries",
            type=positive_int,
            default=default.max_entries,
            metavar="N",
            help=f"Most entries a zip may hold (default: {default.max_entries}).",
        )
        limits.add_argument(
            "--max-entry-size",
            type=parse_size,
            default=default.max_entry_bytes,
            metavar="SIZE",
            help=f"Most one zip entry may unpack to (default: {format_size(default.max_entry_bytes)}).",
        )
        limits.add_argument(
            "--max-compression-ratio",
            type=positive_int,
            default=default.max_compression_ratio,
            metavar="N",
            help=(
                "Most a zip entry larger than 1M may expand relative to its stored size "
                f"(default: {default.max_compression_ratio})."
            ),
        )
        limits.add_argument(
            "--max-line-size",
            type=parse_size,
            default=default.max_line_bytes,
            metavar="SIZE",
            help=f"Longest JSON-lines record, newline included (default: {format_size(default.max_line_bytes)}).",
        )
        limits.add_argument(
            "--max-json-size",
            type=parse_size,
            default=default.max_json_bytes,
            metavar="SIZE",
            help=(
                "Largest manifest.json, lookups.json or public_access.json "
                f"(default: {format_size(default.max_json_bytes)})."
            ),
        )

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> BundleSourceAdapter:
        """Build the adapter from ``--path`` and the limits.

        ``--source-instance`` defaults to the source the manifest names.
        """
        limits = BundleLimits(
            max_unpacked_bytes=args.max_unpacked_size,
            max_entries=args.max_entries,
            max_entry_bytes=args.max_entry_size,
            max_compression_ratio=args.max_compression_ratio,
            max_line_bytes=args.max_line_size,
            max_json_bytes=args.max_json_size,
        )
        files_path = getattr(args, "files_path", None)
        links_file = getattr(args, "links_file", None)
        return cls(
            args.path,
            source_instance=args.source_instance,
            limits=limits,
            files_path=Path(files_path) if files_path else None,
            links_file=Path(links_file) if links_file else None,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def validate(self) -> ValidationReport:
        """Check the bundle without importing it, and return the report."""
        try:
            return self._open()
        finally:
            await self.close()

    async def connect(self) -> None:
        """Open and check the bundle. Raises :class:`BundleInvalidError` if it fails the check."""
        report = self._open()
        if not report.ok:
            await self.close()
            raise BundleInvalidError(report)
        # Declared from what the bundle holds, so an import with passwords
        # checks those hashers before it writes anything.
        self.credential_algorithms = tuple(sorted(self.index.credential_algorithms))  # type: ignore[misc]
        logger.info("Bundle %s is valid: %s", self.path, _summarise(report))

    async def close(self) -> None:
        self._close_container()

    def _close_container(self) -> None:
        if self._container is not None:
            self._container.close()
            self._container = None
        self._index = None

    def _open(self) -> ValidationReport:
        """Open and scan the bundle, always returning a report.

        Whatever goes wrong is reported rather than raised, and on any error
        the bundle is closed again, so no unpacked copy is left behind.
        """
        report = ValidationReport(path=str(self.path))
        self.report = report
        try:
            self._container = open_bundle(self.path, report, self.limits, self.files_path)
            if self._container is not None:
                self._index = scan_bundle(
                    self._container.root, report, self.limits, files_dir=self._container.files_dir
                )
        except Exception as exc:  # noqa: BLE001 - a hostile bundle must give a report, not a traceback
            logger.debug("Reading bundle %s failed", self.path, exc_info=True)
            report.error(ProblemCode.UNREADABLE, self.path.name, f"The bundle could not be read: {exc!r}")
        if not report.ok:
            self._close_container()
        return report

    @property
    def index(self) -> BundleIndex:
        if self._index is None:
            raise RuntimeError("BundleSourceAdapter.connect() must be called before reading the bundle")
        return self._index

    def source_notes(self) -> Iterator[tuple[str, str]]:
        """What the operator should know about the bundle itself, for the import summary."""
        for item in self.index.manifest.get("not_carried_over") or []:
            line = item["what"]
            if item.get("count") is not None:
                line = f"{line} ({item['count']})"
            if item.get("entity_type") and item.get("source_ref"):
                line = f"{item['entity_type']} {item['source_ref']}: {line}"
            yield NOTE_NOT_CARRIED_OVER, line
        for stem, count in sorted(self.index.extra.items()):
            yield NOTE_EXTRA, f"{stem}: {count}"
        if self.report is not None:
            for problem in self.report.warnings:
                yield NOTE_VALIDATION_WARNINGS, problem.format()

    # ------------------------------------------------------------------
    # Reading records back
    # ------------------------------------------------------------------

    def _path(self, record_file: RecordFile) -> Path:
        return self.index.root / record_file.name

    def _all(self, record_file: RecordFile, cls: type[T]) -> Iterator[T]:
        """Read every record of a file, in file order."""
        path = self._path(record_file)
        if not path.exists():
            return
        for _, _, raw in iter_lines(path, self.limits.max_line_bytes):
            # The scan already refused a bundle with an over-long line.
            if raw is not None and raw.strip():
                yield from_record(cls, loads(raw))

    def _at(self, record_file: RecordFile, offsets: Iterable[int], cls: type[T]) -> Iterator[T]:
        """Read the records starting at *offsets*, in that order."""
        offsets = list(offsets)
        if not offsets:
            return
        with open_regular(self._path(record_file)) as handle:
            for offset in offsets:
                handle.seek(offset)
                yield from_record(cls, loads(handle.readline(self.limits.max_line_bytes + 1)))

    async def _stream(self, records: Iterator[T]) -> AsyncIterator[T]:
        for record in records:
            yield record

    def _scoped(self, record_file: RecordFile, project_ref: str, cls: type[T]) -> AsyncIterator[T]:
        return self._stream(self._at(record_file, self.index.offsets(record_file, project_ref), cls))

    # ------------------------------------------------------------------
    # Streams
    # ------------------------------------------------------------------

    async def extract_lookups(self) -> IRLookups:
        lookups = self.index.lookups
        built: dict[str, list[Any]] = {
            name: [from_record(LOOKUP_TYPES[name], item) for item in lookups.get(name) or []] for name in LOOKUP_LISTS
        }
        return IRLookups(**built)

    def extract_users(self) -> AsyncIterator[IRUser]:
        return self._stream(self._all(bundle_format.USERS, IRUser))

    def extract_groups(self) -> AsyncIterator[IRGroup]:
        return self._stream(self._all(bundle_format.GROUPS, IRGroup))

    def extract_projects(self) -> AsyncIterator[IRProject]:
        """Yield projects parents first, whatever order the file has them in."""
        return self._stream(self._at(bundle_format.PROJECTS, self.index.projects, IRProject))

    def extract_custom_fields(self) -> AsyncIterator[IRCustomField]:
        return self._stream(self._all(bundle_format.CUSTOM_FIELDS, IRCustomField))

    def extract_memberships(self, project_ref: str) -> AsyncIterator[IRMembership]:
        return self._scoped(bundle_format.MEMBERSHIPS, project_ref, IRMembership)

    def extract_versions(self, project_ref: str) -> AsyncIterator[IRVersion]:
        return self._scoped(bundle_format.VERSIONS, project_ref, IRVersion)

    def extract_categories(self, project_ref: str) -> AsyncIterator[IRCategory]:
        return self._scoped(bundle_format.CATEGORIES, project_ref, IRCategory)

    def extract_sprints(self, project_ref: str) -> AsyncIterator[IRSprint]:
        return self._scoped(bundle_format.SPRINTS, project_ref, IRSprint)

    def extract_issues(self, project_ref: str) -> AsyncIterator[IRIssue]:
        """Yield a project's issues parents first."""
        return self._scoped(bundle_format.ISSUES, project_ref, IRIssue)

    def extract_journals(self, project_ref: str) -> AsyncIterator[IRJournalEntry]:
        """Yield a project's journal entries oldest first."""
        return self._scoped(bundle_format.JOURNALS, project_ref, IRJournalEntry)

    def extract_relations(self, project_ref: str) -> AsyncIterator[IRRelation]:
        """Yield the relations whose ``from`` issue is in the project.

        A relation whose ``from`` issue is outside the bundle comes with its ``to`` issue's project.
        """
        return self._scoped(bundle_format.RELATIONS, project_ref, IRRelation)

    def extract_watchers(self, project_ref: str) -> AsyncIterator[IRWatcher]:
        return self._stream(self._at(bundle_format.WATCHERS, self.index.issue_watchers.get(project_ref, []), IRWatcher))

    def extract_wiki_watchers(self, project_ref: str) -> AsyncIterator[IRWatcher]:
        return self._stream(self._at(bundle_format.WATCHERS, self.index.wiki_watchers.get(project_ref, []), IRWatcher))

    def extract_attachments(self, project_ref: str) -> AsyncIterator[IRAttachment]:
        return self._scoped(bundle_format.ATTACHMENTS, project_ref, IRAttachment)

    def extract_wiki_pages(self, project_ref: str) -> AsyncIterator[IRWikiPage]:
        """Yield a project's wiki pages parents first, each with its history."""
        return self._scoped(bundle_format.WIKI_PAGES, project_ref, IRWikiPage)

    def extract_wiki_redirects(self, project_ref: str) -> AsyncIterator[IRWikiRedirect]:
        return self._scoped(bundle_format.WIKI_REDIRECTS, project_ref, IRWikiRedirect)

    def extract_time_entries(self, project_ref: str) -> AsyncIterator[IRTimeEntry]:
        return self._scoped(bundle_format.TIME_ENTRIES, project_ref, IRTimeEntry)

    async def extract_public_access(self) -> IRPublicAccess | None:
        """Return the bundle's public access, or ``None`` when it does not describe any."""
        data = self.index.public_access
        return from_record(IRPublicAccess, data) if data is not None else None

    def resolve_attachment_path(self, attachment: IRAttachment) -> Path:
        """Return ``files/<sha256>`` for *attachment*, in the separate files directory if one was given.

        The name was checked to be a hash when the bundle was read, so it
        cannot step outside the directory; checked again here all the same.
        """
        if not SHA256_RE.match(attachment.storage_key):
            raise ValueError(f"Not a stored file name: {attachment.storage_key!r}")
        files_dir = self.index.files_dir or self.index.root / FILES_DIR
        return files_dir / attachment.storage_key


def _source_from_manifest(path: Path, limits: BundleLimits) -> tuple[str, str]:
    """Return the manifest's ``(system, instance)``, or ``("bundle", "bundle")`` if it cannot be read.

    Read before the bundle is opened, because the run options are built from
    the adapter before ``connect``. A manifest that cannot be read, or names
    a reserved system, is refused by validation before anything is written;
    the fallback only has to get that far. Read within the same size limit as
    the scan applies, from a zip too: the archive has not been checked yet.
    """
    fallback = (BundleSourceAdapter.source_system, BundleSourceAdapter.source_system)
    try:
        if path.is_dir():
            with open_regular(path / MANIFEST) as handle:
                raw = handle.read(limits.max_json_bytes + 1)
        else:
            with ZipFile(path) as archive, archive.open(MANIFEST) as handle:
                raw = handle.read(limits.max_json_bytes + 1)
        if len(raw) > limits.max_json_bytes:
            return fallback
        source = loads(raw)["source"]
        system, instance = source["system"], source.get("instance")
    except (OSError, BadZipFile, KeyError, TypeError, ValueError, RuntimeError):
        return fallback
    if not isinstance(system, str) or not system:
        return fallback
    # The schema bounds both; cut to the columns all the same, since the scan
    # that enforces it has not run yet.
    system = system[:30]
    return system, (str(instance) if instance else system)[:255]


def _summarise(report: ValidationReport) -> str:
    counts = ", ".join(f"{count} {name}" for name, count in sorted(report.records.items()) if count)
    return counts or "empty"
