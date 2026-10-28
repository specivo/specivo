"""The ``bundle`` source: import from an import bundle on disk.

A bundle is what an importer outside Specivo writes (see
``docs/import-bundle/v1.md``), so this adapter is the one way in for every
system that has an importer, Specivo knowing nothing about any of them.

``connect`` opens the bundle (unpacking a zip into a temporary directory),
then checks all of it before the first phase runs: an import never starts on a
bundle that fails validation. The same check is what ``--validate`` prints.

Records are read back from the files by byte offset, one at a time, in the
order the scan worked out; nothing holds a whole file in memory.
"""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import AsyncIterator, Iterable, Iterator
from pathlib import Path
from typing import Any, ClassVar, TypeVar
from zipfile import BadZipFile, ZipFile

from specivo.importers.bundle import format as bundle_format
from specivo.importers.bundle.codec import from_record
from specivo.importers.bundle.container import BundleContainer, open_bundle
from specivo.importers.bundle.format import FILES_DIR, LOOKUP_LISTS, MANIFEST, SHA256_RE, RecordFile
from specivo.importers.bundle.report import ValidationReport
from specivo.importers.bundle.scan import BundleIndex, scan_bundle
from specivo.importers.core.ir import (
    IRActivity,
    IRAttachment,
    IRCategory,
    IRCustomField,
    IRGroup,
    IRIssue,
    IRJournalEntry,
    IRLookups,
    IRMembership,
    IRPriority,
    IRProject,
    IRPublicAccess,
    IRRelation,
    IRRole,
    IRStatus,
    IRTimeEntry,
    IRTracker,
    IRUser,
    IRVersion,
    IRWatcher,
    IRWikiPage,
    IRWikiRedirect,
)
from specivo.importers.core.placeholders import REFERENCE_STYLE_PLACEHOLDERS
from specivo.importers.core.source import BaseSourceAdapter

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Report sections.
NOTE_NOT_CARRIED_OVER = "left_behind_by_the_exporter"
NOTE_EXTRA = "records_with_source_data_kept_only_in_the_bundle"
NOTE_VALIDATION_WARNINGS = "bundle_validation_warnings"

# How many problems an import that refuses a bundle puts in its error.
_ERRORS_SHOWN = 20

_LOOKUP_TYPES: dict[str, type] = {
    "trackers": IRTracker,
    "statuses": IRStatus,
    "priorities": IRPriority,
    "activities": IRActivity,
    "roles": IRRole,
}


class BundleInvalidError(ValueError):
    """The bundle failed validation, so nothing was imported."""

    def __init__(self, report: ValidationReport) -> None:
        self.report = report
        super().__init__(f"The bundle is not valid; nothing was imported.\n{report.format_text(limit=_ERRORS_SHOWN)}")


class BundleSourceAdapter(BaseSourceAdapter):
    """Reads an import bundle: a directory, or a ``.zip`` of one.

    The identifier map is kept under the ``bundle`` source, scoped by the
    instance the manifest names (``<system>:<instance>``), so two bundles from
    the same installation share one map — which is what lets a later bundle
    add to an earlier import — while bundles from different systems never
    collide.
    """

    source_system: ClassVar[str] = "bundle"
    description: ClassVar[str] = "An import bundle (directory or .zip) written by an importer"
    reference_style: ClassVar[str] = REFERENCE_STYLE_PLACEHOLDERS

    def __init__(self, path: str | Path, source_instance: str | None = None) -> None:
        self.path = Path(path)
        super().__init__(source_instance or _instance_from_manifest(self.path))
        self._container: BundleContainer | None = None
        self._index: BundleIndex | None = None
        self.report: ValidationReport | None = None

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

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> BundleSourceAdapter:
        """Build the adapter from ``--path``.

        ``--source-instance`` defaults to the source the manifest names.
        """
        return cls(args.path, source_instance=args.source_instance)

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
        if self._container is not None:
            self._container.close()
            self._container = None
        self._index = None

    def _open(self) -> ValidationReport:
        report = ValidationReport(path=str(self.path))
        self.report = report
        self._container = open_bundle(self.path, report)
        if self._container is not None:
            self._index = scan_bundle(self._container.root, report)
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
        with path.open("rb") as handle:
            for raw in handle:
                if raw.strip():
                    yield from_record(cls, json.loads(raw))

    def _at(self, record_file: RecordFile, offsets: Iterable[int], cls: type[T]) -> Iterator[T]:
        """Read the records starting at *offsets*, in that order."""
        offsets = list(offsets)
        if not offsets:
            return
        with self._path(record_file).open("rb") as handle:
            for offset in offsets:
                handle.seek(offset)
                yield from_record(cls, json.loads(handle.readline()))

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
            name: [from_record(_LOOKUP_TYPES[name], item) for item in lookups.get(name) or []] for name in LOOKUP_LISTS
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

    def extract_issues(self, project_ref: str) -> AsyncIterator[IRIssue]:
        """Yield a project's issues parents first."""
        return self._scoped(bundle_format.ISSUES, project_ref, IRIssue)

    def extract_journals(self, project_ref: str) -> AsyncIterator[IRJournalEntry]:
        """Yield a project's journal entries oldest first."""
        return self._scoped(bundle_format.JOURNALS, project_ref, IRJournalEntry)

    def extract_relations(self, project_ref: str) -> AsyncIterator[IRRelation]:
        """Yield the relations whose ``from`` issue is in the project."""
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
        """Return ``files/<sha256>`` for *attachment*.

        The name was checked to be a hash when the bundle was read, so it
        cannot step outside the bundle; checked again here all the same.
        """
        if not SHA256_RE.match(attachment.storage_key):
            raise ValueError(f"Not a stored file name: {attachment.storage_key!r}")
        return self.index.root / FILES_DIR / attachment.storage_key


def _instance_from_manifest(path: Path) -> str:
    """Return ``<system>:<instance>`` from the bundle's manifest, or ``bundle`` if it cannot be read.

    Read before the bundle is opened, because the run options are built from
    the adapter before ``connect``. A manifest that cannot be read is
    reported by validation; the fallback only has to get that far.
    """
    try:
        if path.is_dir():
            manifest = json.loads((path / MANIFEST).read_bytes())
        else:
            with ZipFile(path) as archive:
                manifest = json.loads(archive.read(MANIFEST))
        source = manifest["source"]
        system, instance = str(source["system"]), source.get("instance")
    except (OSError, BadZipFile, KeyError, TypeError, ValueError):
        return BundleSourceAdapter.source_system
    return (f"{system}:{instance}" if instance else system)[:255]


def _summarise(report: ValidationReport) -> str:
    counts = ", ".join(f"{count} {name}" for name, count in sorted(report.records.items()) if count)
    return counts or "empty"
