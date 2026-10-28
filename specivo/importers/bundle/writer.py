"""Write IR to an import bundle.

Small on purpose. It exists so tests can build bundles from IR, and so the
existing Redmine adapter can be exported and the result compared with a
direct import. Importers outside core use the public SDK, which validates as it
writes; this writer trusts its input and leaves checking to the reader.

:func:`export_source` walks any :class:`SourceAdapter` and writes everything it
yields, converting text to Markdown on the way, which is the job an importer
does.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import zipfile
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from types import TracebackType
from typing import IO, Any

from specivo.importers.bundle.codec import to_record
from specivo.importers.bundle.format import (
    FILES_DIR,
    FORMAT_NAME,
    FORMAT_VERSION,
    LOOKUP_LISTS,
    LOOKUPS,
    MANIFEST,
    PUBLIC_ACCESS,
    RECORD_FILES,
    RecordFile,
)
from specivo.importers.core.converter import ConversionContext
from specivo.importers.core.ir import (
    IRAttachment,
    IRIssue,
    IRJournalEntry,
    IRLookups,
    IRPublicAccess,
    IRWikiPage,
    ValueKind,
)
from specivo.importers.core.source import SourceAdapter

_FILE_FOR: dict[type, RecordFile] = {record_file.ir_type: record_file for record_file in RECORD_FILES}
_HASH_CHUNK = 1024 * 1024


def _generator_version() -> str:
    try:
        return version("specivo")
    except PackageNotFoundError:  # pragma: no cover - running from an uninstalled checkout
        return "0"


class BundleWriter:
    """Writes one bundle, to a directory or, for a ``*.zip`` target, an archive.

    Use as a context manager, or call :meth:`close`: the manifest is written
    last, with the counts of what was added.
    """

    def __init__(
        self,
        target: str | Path,
        *,
        source_system: str,
        source_instance: str,
        source_version: str | None = None,
        exported_at: datetime | None = None,
    ) -> None:
        self.target = Path(target)
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        if self.target.suffix == ".zip":
            self._temporary = tempfile.TemporaryDirectory(prefix="specivo-bundle-")
            self.root = Path(self._temporary.name)
        else:
            self.target.mkdir(parents=True, exist_ok=True)
            if any(self.target.iterdir()):
                raise FileExistsError(f"{self.target} is not empty")
            self.root = self.target

        self._source: dict[str, Any] = {"system": source_system, "instance": source_instance}
        if source_version:
            self._source["version"] = source_version
        self._exported_at = exported_at or datetime.now(UTC)
        self._handles: dict[str, IO[str]] = {}
        self._counts: Counter[str] = Counter()
        self._lookups: dict[str, Any] = {}
        self._public_access: dict[str, Any] | None = None
        self._not_carried_over: list[dict[str, Any]] = []
        self._closed = False

    def __enter__(self) -> BundleWriter:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc_type is None:
            self.close()
        else:
            self._abandon()

    # ------------------------------------------------------------------
    # Content
    # ------------------------------------------------------------------

    def write_lookups(self, lookups: IRLookups) -> None:
        self._lookups = {name: [to_record(item) for item in getattr(lookups, name)] for name in LOOKUP_LISTS}
        for name in LOOKUP_LISTS:
            self._counts[name] = len(self._lookups[name])

    def write_public_access(self, access: IRPublicAccess) -> None:
        self._public_access = to_record(access)

    def add(self, record: Any) -> None:
        """Append one IR record to the file for its type.

        An attachment's ``storage_key`` must be the hash :meth:`add_file`
        returned for its bytes.
        """
        record_file = _FILE_FOR[type(record)]
        if isinstance(record, IRAttachment) and not (self.root / FILES_DIR / record.storage_key).is_file():
            raise ValueError(f"Attachment {record.source_ref}: add its file first and use the hash as storage_key")

        handle = self._handles.get(record_file.name)
        if handle is None:
            handle = (self.root / record_file.name).open("w", encoding="utf-8")
            self._handles[record_file.name] = handle
        handle.write(json.dumps(to_record(record), ensure_ascii=False, separators=(",", ":")))
        handle.write("\n")

        self._counts[record_file.stem] += 1
        if isinstance(record, IRWikiPage):
            self._counts["wiki_versions"] += len(record.versions)

    def add_file(self, source: str | Path) -> str:
        """Store a file's bytes under ``files/`` and return their SHA-256."""
        files_dir = self.root / FILES_DIR
        files_dir.mkdir(exist_ok=True)
        digest = hashlib.sha256()
        with tempfile.NamedTemporaryFile(dir=files_dir, delete=False) as staged, Path(source).open("rb") as handle:
            while chunk := handle.read(_HASH_CHUNK):
                digest.update(chunk)
                staged.write(chunk)
        sha256 = digest.hexdigest()
        stored = files_dir / sha256
        if stored.exists():
            Path(staged.name).unlink()
        else:
            Path(staged.name).replace(stored)
        return sha256

    def add_bytes(self, data: bytes) -> str:
        """Store *data* under ``files/`` and return its SHA-256."""
        files_dir = self.root / FILES_DIR
        files_dir.mkdir(exist_ok=True)
        sha256 = hashlib.sha256(data).hexdigest()
        (files_dir / sha256).write_bytes(data)
        return sha256

    def not_carried_over(
        self,
        what: str,
        *,
        count: int | None = None,
        entity_type: str | None = None,
        source_ref: str | None = None,
    ) -> None:
        """Record something the source had that the bundle does not."""
        item: dict[str, Any] = {"what": what}
        if count is not None:
            item["count"] = count
        if entity_type is not None:
            item["entity_type"] = entity_type
        if source_ref is not None:
            item["source_ref"] = source_ref
        self._not_carried_over.append(item)

    # ------------------------------------------------------------------
    # Finishing
    # ------------------------------------------------------------------

    def close(self) -> Path:
        """Write the manifest and the small JSON files, and return the bundle's path."""
        if self._closed:
            return self.target
        for handle in self._handles.values():
            handle.close()
        if self._lookups:
            _write_json(self.root / LOOKUPS, self._lookups)
        if self._public_access is not None:
            _write_json(self.root / PUBLIC_ACCESS, self._public_access)

        manifest: dict[str, Any] = {
            "format": FORMAT_NAME,
            "format_version": FORMAT_VERSION,
            "source": self._source,
            "generator": {"name": "specivo", "version": _generator_version()},
            "exported_at": self._exported_at.isoformat(),
            "counts": dict(sorted(self._counts.items())),
        }
        if self._not_carried_over:
            manifest["not_carried_over"] = self._not_carried_over
        _write_json(self.root / MANIFEST, manifest)

        if self._temporary is not None:
            self.target.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(self.target, "w", zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(self.root.rglob("*")):
                    if path.is_file():
                        archive.write(path, path.relative_to(self.root).as_posix())
            self._temporary.cleanup()
        self._closed = True
        return self.target

    def _abandon(self) -> None:
        for handle in self._handles.values():
            handle.close()
        if self._temporary is not None:
            self._temporary.cleanup()
        self._closed = True


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# Exporting a source
# --------------------------------------------------------------------------


class TextField(StrEnum):
    """The Markdown fields an export converts."""

    ISSUE_DESCRIPTION = "issue.description"
    JOURNAL_NOTES = "journal.notes"
    WIKI_TEXT = "wiki.text"


TextConverter = Callable[[str, TextField], str]


def source_converter(adapter: SourceAdapter) -> TextConverter:
    """Return a converter that turns *adapter*'s markup into Markdown, as its import would."""
    converter = adapter.content_converter()
    context = ConversionContext(source_format=getattr(adapter, "source_format", "markdown"))

    def convert(text: str, field: TextField) -> str:
        return converter.convert(text, context)

    return convert


async def export_source(
    adapter: SourceAdapter,
    writer: BundleWriter,
    convert_text: TextConverter | None = None,
) -> None:
    """Write everything *adapter* yields to *writer*.

    *adapter* must be connected. Text is converted with *convert_text*, by
    default the adapter's own converter. An attachment whose file cannot be
    read is left out and listed in the manifest's ``not_carried_over``.
    """
    convert = convert_text or source_converter(adapter)

    writer.write_lookups(await adapter.extract_lookups())
    async for user in adapter.extract_users():
        writer.add(user)
    async for group in adapter.extract_groups():
        writer.add(group)

    project_refs: list[str] = []
    async for project in adapter.extract_projects():
        writer.add(project)
        project_refs.append(project.source_ref)
        for module in _call(adapter, "dropped_modules", project.source_ref) or []:
            writer.not_carried_over(f"module {module}", entity_type="project", source_ref=project.source_ref)

    async for custom_field in adapter.extract_custom_fields():
        writer.add(custom_field)

    relations: set[str] = set()
    for project_ref in project_refs:
        async for membership in adapter.extract_memberships(project_ref):
            writer.add(membership)
        async for project_version in adapter.extract_versions(project_ref):
            writer.add(project_version)
        async for category in adapter.extract_categories(project_ref):
            writer.add(category)
        async for issue in adapter.extract_issues(project_ref):
            writer.add(_converted_issue(issue, convert))
        async for journal in adapter.extract_journals(project_ref):
            writer.add(_converted_journal(journal, convert))
        async for relation in adapter.extract_relations(project_ref):
            # A relation across two projects is yielded under both.
            if relation.source_ref not in relations:
                relations.add(relation.source_ref)
                writer.add(relation)
        async for watcher in adapter.extract_watchers(project_ref):
            writer.add(watcher)
        async for page in adapter.extract_wiki_pages(project_ref):
            writer.add(_converted_page(page, convert))
        wiki_watchers = getattr(adapter, "extract_wiki_watchers", None)
        if callable(wiki_watchers):
            async for watcher in wiki_watchers(project_ref):
                writer.add(watcher)
        redirects = getattr(adapter, "extract_wiki_redirects", None)
        if callable(redirects):
            async for redirect in redirects(project_ref):
                writer.add(redirect)
        async for attachment in adapter.extract_attachments(project_ref):
            _add_attachment(adapter, writer, attachment)
        async for entry in adapter.extract_time_entries(project_ref):
            writer.add(entry)

    public_access = getattr(adapter, "extract_public_access", None)
    if callable(public_access):
        access = await public_access()
        if access is not None:
            writer.write_public_access(access)


def _call(adapter: Any, name: str, *args: Any) -> Any:
    method = getattr(adapter, name, None)
    return method(*args) if callable(method) else None


def _converted_issue(issue: IRIssue, convert: TextConverter) -> IRIssue:
    if issue.description:
        issue.description = convert(issue.description, TextField.ISSUE_DESCRIPTION)
    for value in issue.custom_values:
        # A ref is a string in a bundle; an adapter may hand over the source's
        # integer key.
        if value.value_kind is not ValueKind.SCALAR:
            value.value = [str(item) for item in value.value] if isinstance(value.value, list) else str(value.value)
    return issue


def _converted_journal(journal: IRJournalEntry, convert: TextConverter) -> IRJournalEntry:
    if journal.notes:
        journal.notes = convert(journal.notes, TextField.JOURNAL_NOTES)
    return journal


def _converted_page(page: IRWikiPage, convert: TextConverter) -> IRWikiPage:
    for revision in page.versions:
        if revision.text:
            revision.text = convert(revision.text, TextField.WIKI_TEXT)
    return page


def _add_attachment(adapter: SourceAdapter, writer: BundleWriter, attachment: IRAttachment) -> None:
    try:
        path = adapter.resolve_attachment_path(attachment)
        attachment.storage_key = writer.add_file(path)
    except (OSError, ValueError) as exc:
        writer.not_carried_over(
            f"attachment {attachment.filename}: {exc}", entity_type="attachment", source_ref=attachment.source_ref
        )
        return
    writer.add(attachment)
