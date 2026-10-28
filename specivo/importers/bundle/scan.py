"""Validate a bundle and index it for loading, in one pass over its files.

Validation and loading need the same walk: every record parsed, every
reference resolved, every stored file hashed. Doing it once means ``--validate``
and the check a real import runs first cannot disagree, and the index the
adapter streams from comes out of it for free.

**What is checked.** Each record against its JSON Schema; ``source_ref``
uniqueness per entity type; every reference to another record, including the
placeholders in text; stored files exist and hash to their names; parent
chains have no cycles; the manifest's counts match the files.

**What is kept in memory.** Never a whole JSON-lines file: each record (one
line, at most ``max_line_bytes``) is parsed, checked and dropped. The three
small JSON files are read whole, each at most ``max_json_bytes``. What stays
is one entry per record — its source ref and the byte offset of its line —
plus the few fields needed to put records in load order (parents, journal
timestamps), so memory grows with the number of records, not their size. The
adapter later reads records back by offset.

**Order.** Files are checked in :data:`~specivo.importers.bundle.format.RECORD_FILES`
order, in which a file only refers to the ones before it. A reference that
cannot be checked yet — a parent further down the same file, a placeholder
naming a wiki page — is kept and checked at the end.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from referencing import Registry, Resource

from specivo.importers.bundle.codec import LOOKUP_TYPES, value_problems
from specivo.importers.bundle.format import (
    ATTACHMENTS,
    CATEGORIES,
    CUSTOM_FIELDS,
    FILES_DIR,
    FORMAT_MAJOR,
    FORMAT_MINOR,
    FORMAT_NAME,
    GROUPS,
    ISSUES,
    JOURNALS,
    LOOKUP_LISTS,
    LOOKUPS,
    MANIFEST,
    MEMBERSHIPS,
    PROJECTS,
    PUBLIC_ACCESS,
    RECORD_FILES,
    RELATIONS,
    RESERVED_SOURCE_SYSTEMS,
    SCHEMA_DIR,
    SHA256_RE,
    TIME_ENTRIES,
    VERSIONS,
    WATCHERS,
    WIKI_PAGES,
    WIKI_REDIRECTS,
    RecordFile,
)
from specivo.importers.bundle.limits import BundleLimits, format_size
from specivo.importers.bundle.report import ProblemCode, ValidationReport
from specivo.importers.core.ir import ContainerKind, EntityType, PrincipalKind, ValueKind
from specivo.importers.core.placeholders import LABELLED_KINDS, find_placeholders

_HASH_CHUNK = 1024 * 1024
# Long values (a whole description) make a schema message unreadable.
_MAX_MESSAGE = 300
_EARLIEST = datetime.min.replace(tzinfo=UTC)
# Read size when skipping the rest of an over-long line.
_SKIP_CHUNK = 1024 * 1024


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------


@cache
def schemas() -> dict[str, dict[str, Any]]:
    """Return every v1 schema by file name."""
    return {path.name: json.loads(path.read_text()) for path in sorted(SCHEMA_DIR.glob("*.schema.json"))}


@cache
def schema_registry() -> Registry[Any]:
    """Return a registry holding every v1 schema under its ``$id``, for ``$ref`` resolution."""
    resources = [(schema["$id"], Resource.from_contents(schema)) for schema in schemas().values()]
    return Registry().with_resources(resources)


@cache
def validator(schema_name: str) -> Draft202012Validator:
    """Return the validator for one schema file."""
    return Draft202012Validator(schemas()[schema_name], registry=schema_registry())


# --------------------------------------------------------------------------
# Ordering
# --------------------------------------------------------------------------


def order_parents_first(parents: dict[str, str | None]) -> tuple[list[str], list[str]]:
    """Return the refs of *parents* ordered so a parent precedes its children.

    Records already in a valid order keep it, so a bundle written parents
    first loads in exactly its own order. A parent that is not among the refs
    counts as absent. Refs caught in a cycle cannot be ordered; they are
    returned second, in their original order.
    """
    emitted: set[str] = set()
    order: list[str] = []
    waiting: dict[str, list[str]] = {}

    def emit(ref: str) -> None:
        stack = [ref]
        while stack:
            current = stack.pop()
            emitted.add(current)
            order.append(current)
            stack.extend(reversed(waiting.pop(current, [])))

    for ref, parent in parents.items():
        if parent is None or parent not in parents or parent in emitted:
            emit(ref)
        else:
            waiting.setdefault(parent, []).append(ref)

    cyclic = [ref for ref in parents if ref not in emitted]
    return order, cyclic


# --------------------------------------------------------------------------
# The index
# --------------------------------------------------------------------------


@dataclass
class BundleIndex:
    """Where every record of a valid bundle is, in the order it loads.

    Offsets are byte offsets of a record's line in its file. ``by_project``
    holds, per project-scoped file, each project's records in load order;
    watchers are split by what they watch, because issue and wiki watchers load
    in different phases.
    """

    root: Path
    manifest: dict[str, Any]
    lookups: dict[str, Any] = field(default_factory=dict)
    public_access: dict[str, Any] | None = None
    projects: list[int] = field(default_factory=list)
    by_project: dict[str, dict[str, list[int]]] = field(default_factory=dict)
    issue_watchers: dict[str, list[int]] = field(default_factory=dict)
    wiki_watchers: dict[str, list[int]] = field(default_factory=dict)
    credential_algorithms: set[str] = field(default_factory=set)
    # Records carrying an ``extra`` object, per file.
    extra: Counter[str] = field(default_factory=Counter)

    def offsets(self, file: RecordFile, project_ref: str) -> list[int]:
        return self.by_project.get(file.name, {}).get(project_ref, [])


def iter_lines(path: Path, max_line_bytes: int) -> Iterator[tuple[int, int, bytes | None]]:
    """Yield ``(line number, byte offset, raw line)`` for each line of *path*.

    A line longer than *max_line_bytes* (newline included) is yielded as
    ``None`` and skipped without being held in memory, so one huge line cannot
    exhaust memory before it is even parsed.
    """
    with path.open("rb") as handle:
        offset = 0
        number = 0
        while raw := handle.readline(max_line_bytes + 1):
            number += 1
            if len(raw) <= max_line_bytes:
                yield number, offset, raw
                offset += len(raw)
                continue
            length = len(raw)
            while not raw.endswith(b"\n"):
                raw = handle.readline(_SKIP_CHUNK)
                if not raw:
                    break
                length += len(raw)
            yield number, offset, None
            offset += length


def loads(raw: bytes | str) -> Any:
    """Parse JSON as the format defines it: no ``NaN`` or ``Infinity``."""
    return json.loads(raw, parse_constant=_reject_constant)


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not a JSON value")


def scan_bundle(root: Path, report: ValidationReport, limits: BundleLimits | None = None) -> BundleIndex | None:
    """Check the bundle unpacked at *root*, recording problems in *report*.

    Returns the index, or ``None`` when the manifest is missing or names a
    format version this reader does not support, in which case nothing else
    was read. A returned index belongs to a loadable bundle only if
    ``report.ok``.
    """
    return _Scanner(root, report, limits or BundleLimits()).run()


# --------------------------------------------------------------------------
# The scanner
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Pending:
    """A reference that can only be checked once later files are read."""

    file: str
    line: int | None
    field: str
    entity_type: EntityType
    ref: str


class _Scanner:
    def __init__(self, root: Path, report: ValidationReport, limits: BundleLimits) -> None:
        self.root = root
        self.report = report
        self.limits = limits
        self.index = BundleIndex(root=root, manifest={})
        self.refs: dict[EntityType, dict[str, int | None]] = {}
        self.done: set[EntityType] = set()
        self.pending: list[_Pending] = []
        # Unknown properties are errors, unless the bundle is a newer minor
        # version: it may add optional fields this reader does not know.
        self.tolerate_unknown = False

        self.project_parents: dict[str, str | None] = {}
        self.project_offsets: dict[str, int] = {}
        self.issue_project: dict[str, str] = {}
        self.issue_nodes: dict[str, dict[str, tuple[str | None, int]]] = {}
        self.page_project: dict[str, str] = {}
        self.page_nodes: dict[str, dict[str, tuple[str | None, int]]] = {}
        self.journal_keys: dict[str, list[tuple[datetime, int, int]]] = {}
        self.custom_fields: dict[str, tuple[str, str]] = {}
        self.redirects: set[tuple[str, str]] = set()
        self.watchers: set[tuple[str, str, str]] = set()
        self.stored_files: dict[str, int | None] = {}

    # -- driver ------------------------------------------------------------

    def run(self) -> BundleIndex | None:
        manifest = self._read_manifest()
        if manifest is None:
            return None
        self.index.manifest = manifest

        self._scan_lookups()
        for record_file in RECORD_FILES:
            self._scan_file(record_file)
        self._scan_public_access()
        self._finish()
        return self.index

    # -- manifest ----------------------------------------------------------

    def _read_manifest(self) -> dict[str, Any] | None:
        data = self._read_json(MANIFEST, required=True)
        if not isinstance(data, dict):
            if data is not None:
                self.report.error(ProblemCode.SCHEMA, MANIFEST, "The manifest must be a JSON object")
            return None

        name, version = data.get("format"), data.get("format_version")
        if name != FORMAT_NAME:
            self.report.error(
                ProblemCode.UNSUPPORTED_VERSION,
                MANIFEST,
                f"Not an import bundle: format is {name!r}, expected {FORMAT_NAME!r}",
                pointer="/format",
            )
            return None
        major, minor = _parse_version(version)
        if major != FORMAT_MAJOR:
            self.report.error(
                ProblemCode.UNSUPPORTED_VERSION,
                MANIFEST,
                f"Format version {version!r} is not supported: this Specivo reads version {FORMAT_MAJOR}.x",
                pointer="/format_version",
            )
            return None
        if minor > FORMAT_MINOR:
            self.tolerate_unknown = True
            self.report.warning(
                ProblemCode.UNSUPPORTED_VERSION,
                MANIFEST,
                f"Format version {version} is newer than this Specivo's {FORMAT_MAJOR}.{FORMAT_MINOR}; "
                "fields it does not know are ignored",
                pointer="/format_version",
            )

        if self._validate(MANIFEST, "manifest.schema.json", data):
            try:
                datetime.fromisoformat(data["exported_at"])
            except ValueError:
                self.report.error(
                    ProblemCode.SCHEMA,
                    MANIFEST,
                    f"{data['exported_at']!r} is not a valid timestamp",
                    pointer="/exported_at",
                )
            system = data["source"]["system"]
            if system in RESERVED_SOURCE_SYSTEMS:
                self.report.error(
                    ProblemCode.SCHEMA,
                    MANIFEST,
                    f"{system!r} is reserved and cannot be a bundle's source system; name the system the data comes "
                    "from (e.g. redmine)",
                    pointer="/source/system",
                )
        return data

    def _check_counts(self) -> None:
        counts = self.index.manifest.get("counts") or {}
        if not isinstance(counts, dict):
            return
        for name, expected in sorted(counts.items()):
            actual = self.report.records.get(name, 0)
            if isinstance(expected, int) and expected != actual:
                self.report.error(
                    ProblemCode.COUNT_MISMATCH,
                    MANIFEST,
                    f"The manifest counts {expected} {name} but the bundle holds {actual}",
                    pointer=f"/counts/{name}",
                )

    # -- plain JSON files --------------------------------------------------

    def _read_json(self, name: str, *, required: bool = False) -> Any:
        path = self.root / name
        if not path.exists():
            if required:
                self.report.error(ProblemCode.MISSING_FILE, name, "The bundle has no manifest")
            return None
        try:
            size = path.stat().st_size
            if size > self.limits.max_json_bytes:
                self.report.error(
                    ProblemCode.TOO_LARGE,
                    name,
                    f"{size} bytes; the limit is {format_size(self.limits.max_json_bytes)} (--max-json-size)",
                )
                return None
            return loads(path.read_bytes())
        except OSError as exc:
            self.report.error(ProblemCode.UNREADABLE, name, f"Could not be read: {exc}")
        except (ValueError, UnicodeDecodeError) as exc:
            self.report.error(ProblemCode.INVALID_JSON, name, f"Not valid JSON: {exc}")
        return None

    def _scan_lookups(self) -> None:
        data = self._read_json(LOOKUPS)
        if data is not None and self._validate(LOOKUPS, "lookups.schema.json", data) and self._lookup_values(data):
            self.index.lookups = data
            for list_name, entity_type in LOOKUP_LISTS.items():
                for position, item in enumerate(data.get(list_name) or []):
                    self._register(LOOKUPS, None, entity_type, item["source_ref"], pointer=f"/{list_name}/{position}")
                    self.report.records[list_name] += 1
        self.done.update(LOOKUP_LISTS.values())

        for position, tracker in enumerate(self.index.lookups.get("trackers") or []):
            self._ref(
                LOOKUPS,
                None,
                f"/trackers/{position}/default_status_ref",
                EntityType.STATUS,
                tracker.get("default_status_ref"),
            )

    def _lookup_values(self, data: dict[str, Any]) -> bool:
        valid = True
        for list_name, cls in LOOKUP_TYPES.items():
            for position, item in enumerate(data.get(list_name) or []):
                for problem in value_problems(cls, item, f"/{list_name}/{position}"):
                    pointer, _, message = problem.partition(": ")
                    self.report.error(ProblemCode.SCHEMA, LOOKUPS, message, pointer=pointer)
                    valid = False
        return valid

    def _scan_public_access(self) -> None:
        data = self._read_json(PUBLIC_ACCESS)
        if data is None or not self._validate(PUBLIC_ACCESS, "public_access.schema.json", data):
            return
        self.index.public_access = data
        for project_ref in data.get("anonymous_permissions") or {}:
            self._ref(PUBLIC_ACCESS, None, f"/anonymous_permissions/{project_ref}", EntityType.PROJECT, project_ref)

    # -- JSON-lines files --------------------------------------------------

    def _scan_file(self, record_file: RecordFile) -> None:
        path = self.root / record_file.name
        if path.exists():
            try:
                self._scan_lines(record_file, path)
            except OSError as exc:
                self.report.error(ProblemCode.UNREADABLE, record_file.name, f"Could not be read: {exc}")
        if record_file.entity_type is not None:
            self.done.add(record_file.entity_type)
        if record_file is WIKI_PAGES:
            self.done.add(EntityType.WIKI_VERSION)

    def _scan_lines(self, record_file: RecordFile, path: Path) -> None:
        check = getattr(self, f"_check_{record_file.stem}")
        for number, offset, raw in iter_lines(path, self.limits.max_line_bytes):
            if raw is None:
                self.report.error(
                    ProblemCode.TOO_LARGE,
                    record_file.name,
                    f"Line is longer than {format_size(self.limits.max_line_bytes)} (--max-line-size); not read",
                    line=number,
                )
                continue
            if not raw.strip():
                continue
            record = self._parse_line(record_file, number, raw)
            if record is None:
                continue
            if record.get("extra"):
                self.index.extra[record_file.stem] += 1
            if record_file.entity_type is not None:
                self._register(record_file.name, number, record_file.entity_type, record["source_ref"])
            check(number, offset, record)

    def _parse_line(self, record_file: RecordFile, number: int, raw: bytes) -> dict[str, Any] | None:
        try:
            record = loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            self.report.error(ProblemCode.INVALID_JSON, record_file.name, f"Not valid JSON: {exc}", line=number)
            return None
        # Counted whether or not it is valid, so a broken record is reported
        # once, not again as a count that does not match the manifest.
        self.report.records[record_file.stem] += 1
        valid = self._validate(record_file.name, record_file.schema, record, line=number)
        if valid:
            for problem in value_problems(record_file.ir_type, record):
                self.report.error(ProblemCode.SCHEMA, record_file.name, problem, line=number)
                valid = False
        if not valid:
            # Still claim the ref, so a record that points at this one is not
            # reported a second time as unresolved.
            if isinstance(record, dict) and isinstance(record.get("source_ref"), str) and record_file.entity_type:
                self._register(record_file.name, number, record_file.entity_type, record["source_ref"])
            return None
        parsed: dict[str, Any] = record
        return parsed

    def _validate(self, file: str, schema_name: str, data: Any, *, line: int | None = None) -> bool:
        """Check *data* against a schema, reporting each failure. True when no error was reported."""
        valid = True
        errors: list[ValidationError] = sorted(validator(schema_name).iter_errors(data), key=lambda e: e.json_path)
        for error in errors:
            pointer = "/" + "/".join(str(part) for part in error.absolute_path)
            where: dict[str, Any] = {"line": line} if line is not None else {"pointer": pointer}
            message = error.message if len(error.message) <= _MAX_MESSAGE else error.message[:_MAX_MESSAGE] + "…"
            if line is not None and pointer != "/":
                message = f"{pointer}: {message}"
            if self.tolerate_unknown and error.validator == "additionalProperties":
                self.report.warning(ProblemCode.SCHEMA, file, message, **where)
                continue
            self.report.error(ProblemCode.SCHEMA, file, message, **where)
            valid = False
        return valid

    # -- references --------------------------------------------------------

    def _register(
        self,
        file: str,
        line: int | None,
        entity_type: EntityType,
        ref: str,
        *,
        pointer: str | None = None,
    ) -> None:
        """Claim *ref* for *entity_type*, reporting a second claim."""
        if ref.endswith("\n"):
            # The schema's pattern cannot refuse this: Python's "$" matches before a final newline.
            where_: dict[str, Any] = {"line": line} if line is not None else {"pointer": pointer}
            self.report.error(ProblemCode.SCHEMA, file, f"source_ref {ref!r} ends in a newline", **where_)
        seen = self.refs.setdefault(entity_type, {})
        if ref in seen:
            first = seen[ref]
            earlier = f" (first at line {first})" if first is not None else ""
            where: dict[str, Any] = {"line": line} if line is not None else {"pointer": pointer}
            self.report.error(
                ProblemCode.DUPLICATE_REF,
                file,
                f"Duplicate source_ref {ref!r} for {entity_type}{earlier}",
                **where,
            )
            return
        seen[ref] = line

    def _known(self, entity_type: EntityType, ref: str) -> bool:
        return ref in self.refs.get(entity_type, {})

    def _ref(self, file: str, line: int | None, field_name: str, entity_type: EntityType, ref: Any) -> None:
        """Check that *ref* names a record, now or once its file has been read."""
        if ref is None:
            return
        pending = _Pending(file, line, field_name, entity_type, str(ref))
        if entity_type in self.done:
            self._resolve(pending)
        else:
            self.pending.append(pending)

    def _resolve(self, pending: _Pending) -> None:
        if self._known(pending.entity_type, pending.ref):
            return
        where: dict[str, Any] = {"line": pending.line} if pending.line is not None else {"pointer": pending.field}
        label = pending.field if pending.line is not None else pending.field.rsplit("/", 1)[-1]
        self.report.error(
            ProblemCode.UNRESOLVED_REF,
            pending.file,
            f"{label}: no {pending.entity_type} with source_ref {pending.ref!r}",
            **where,
        )

    def _text(self, file: str, line: int, field_name: str, text: str | None) -> None:
        """Check the placeholders in a Markdown field."""
        for placeholder in find_placeholders(text):
            if placeholder.label is not None and placeholder.kind not in LABELLED_KINDS:
                self.report.error(
                    ProblemCode.INVALID_REF,
                    file,
                    f"{field_name}: a {placeholder.kind} placeholder takes no label",
                    line=line,
                )
            elif placeholder.label is not None and "]]" in placeholder.label:
                self.report.error(
                    ProblemCode.INVALID_REF,
                    file,
                    f"{field_name}: a placeholder's label may not contain ']]'",
                    line=line,
                )
            where = f"{field_name} {{{{{placeholder.kind}:…}}}}"
            self._ref(file, line, where, placeholder.entity_type, placeholder.ref)

    # -- per-file checks ---------------------------------------------------
    #
    # Called with records that passed their schema, so every required
    # property is there and every value has the right type.

    def _check_users(self, line: int, offset: int, record: dict[str, Any]) -> None:
        credential = record.get("credential")
        if credential:
            self.index.credential_algorithms.add(credential["algorithm"])

    def _check_groups(self, line: int, offset: int, record: dict[str, Any]) -> None:
        for ref in record.get("member_refs") or []:
            self._ref(GROUPS.name, line, "member_refs", EntityType.USER, ref)

    def _check_projects(self, line: int, offset: int, record: dict[str, Any]) -> None:
        ref = record["source_ref"]
        self.project_parents.setdefault(ref, record.get("parent_ref"))
        self.project_offsets.setdefault(ref, offset)
        self._ref(PROJECTS.name, line, "parent_ref", EntityType.PROJECT, record.get("parent_ref"))

    def _in_project(self, record_file: RecordFile, project_ref: str, offset: int) -> None:
        self.index.by_project.setdefault(record_file.name, {}).setdefault(project_ref, []).append(offset)

    def _scoped(self, record_file: RecordFile, line: int, offset: int, record: dict[str, Any]) -> str:
        project_ref: str = record["project_ref"]
        self._ref(record_file.name, line, "project_ref", EntityType.PROJECT, project_ref)
        return project_ref

    def _check_memberships(self, line: int, offset: int, record: dict[str, Any]) -> None:
        project_ref = self._scoped(MEMBERSHIPS, line, offset, record)
        self._in_project(MEMBERSHIPS, project_ref, offset)
        principal = EntityType.GROUP if record["principal_kind"] == PrincipalKind.GROUP else EntityType.USER
        self._ref(MEMBERSHIPS.name, line, "principal_ref", principal, record["principal_ref"])
        for ref in record.get("role_refs") or []:
            self._ref(MEMBERSHIPS.name, line, "role_refs", EntityType.ROLE, ref)

    def _check_versions(self, line: int, offset: int, record: dict[str, Any]) -> None:
        self._in_project(VERSIONS, self._scoped(VERSIONS, line, offset, record), offset)

    def _check_categories(self, line: int, offset: int, record: dict[str, Any]) -> None:
        self._in_project(CATEGORIES, self._scoped(CATEGORIES, line, offset, record), offset)
        self._ref(CATEGORIES.name, line, "assigned_to_ref", EntityType.USER, record.get("assigned_to_ref"))

    def _check_custom_fields(self, line: int, offset: int, record: dict[str, Any]) -> None:
        key = record["key"]
        if any(known == key for known, _ in self.custom_fields.values()):
            self.report.error(
                ProblemCode.INVALID_REF,
                CUSTOM_FIELDS.name,
                f"Another custom field already stores its values under key {key!r}",
                line=line,
            )
        self.custom_fields.setdefault(record["source_ref"], (key, record["field_format"]))
        for ref in record.get("tracker_refs") or []:
            self._ref(CUSTOM_FIELDS.name, line, "tracker_refs", EntityType.TRACKER, ref)
        for ref in record.get("project_refs") or []:
            self._ref(CUSTOM_FIELDS.name, line, "project_refs", EntityType.PROJECT, ref)

    def _check_issues(self, line: int, offset: int, record: dict[str, Any]) -> None:
        name = ISSUES.name
        ref = record["source_ref"]
        project_ref = self._scoped(ISSUES, line, offset, record)
        self.issue_project.setdefault(ref, project_ref)
        self.issue_nodes.setdefault(project_ref, {}).setdefault(ref, (record.get("parent_ref"), offset))

        self._ref(name, line, "tracker_ref", EntityType.TRACKER, record["tracker_ref"])
        self._ref(name, line, "status_ref", EntityType.STATUS, record["status_ref"])
        self._ref(name, line, "priority_ref", EntityType.PRIORITY, record["priority_ref"])
        self._ref(name, line, "author_ref", EntityType.USER, record.get("author_ref"))
        self._ref(name, line, "parent_ref", EntityType.ISSUE, record.get("parent_ref"))
        self._ref(name, line, "category_ref", EntityType.CATEGORY, record.get("category_ref"))
        self._ref(name, line, "fixed_version_ref", EntityType.VERSION, record.get("fixed_version_ref"))

        assignee: str | None = record.get("assigned_to_ref")
        if (
            assignee is not None
            and self._known(EntityType.GROUP, assignee)
            and not self._known(EntityType.USER, assignee)
        ):
            self.report.warning(
                ProblemCode.INVALID_REF,
                name,
                f"assigned_to_ref: {assignee!r} is a group; Specivo assigns issues to people only, "
                "so the issue will be left unassigned",
                line=line,
            )
        else:
            self._ref(name, line, "assigned_to_ref", EntityType.USER, assignee)

        for value in record.get("custom_values") or []:
            self._check_custom_value(line, value)
        self._text(name, line, "description", record.get("description"))

    def _check_custom_value(self, line: int, value: dict[str, Any]) -> None:
        name = ISSUES.name
        field_ref = value["field_ref"]
        self._ref(name, line, "custom_values.field_ref", EntityType.CUSTOM_FIELD, field_ref)
        definition = self.custom_fields.get(field_ref)
        if definition is not None and definition[0] != value["key"]:
            self.report.error(
                ProblemCode.INVALID_REF,
                name,
                f"custom_values: key {value['key']!r} differs from field {field_ref!r}'s key {definition[0]!r}",
                line=line,
            )

        kind = value.get("value_kind", ValueKind.SCALAR)
        if kind == ValueKind.SCALAR:
            return
        target = EntityType.USER if kind == ValueKind.USER_REF else EntityType.VERSION
        raw = value["value"]
        refs = raw if isinstance(raw, list) else [raw]
        for ref in refs:
            if not isinstance(ref, str):
                self.report.error(
                    ProblemCode.INVALID_REF,
                    name,
                    f"custom_values: a {kind} value must be a source_ref string, got {ref!r}",
                    line=line,
                )
                continue
            self._ref(name, line, "custom_values.value", target, ref)

    def _issue_scope(self, record_file: RecordFile, line: int, field_name: str, issue_ref: str) -> str | None:
        self._ref(record_file.name, line, field_name, EntityType.ISSUE, issue_ref)
        return self.issue_project.get(issue_ref)

    def _check_journals(self, line: int, offset: int, record: dict[str, Any]) -> None:
        project_ref = self._issue_scope(JOURNALS, line, "issue_ref", record["issue_ref"])
        self._ref(JOURNALS.name, line, "user_ref", EntityType.USER, record.get("user_ref"))
        self._text(JOURNALS.name, line, "notes", record.get("notes"))
        for detail in record.get("details") or []:
            # A description change is Markdown, and its placeholders are expanded.
            if detail["property"] == "attr" and detail["prop_key"] == "description":
                self._text(JOURNALS.name, line, "details.old_value", detail.get("old_value"))
                self._text(JOURNALS.name, line, "details.new_value", detail.get("new_value"))
        if project_ref is not None:
            created = record.get("created_at")
            key = datetime.fromisoformat(created) if created else _EARLIEST
            self.journal_keys.setdefault(project_ref, []).append((key, line, offset))

    def _check_relations(self, line: int, offset: int, record: dict[str, Any]) -> None:
        project_ref = self._issue_scope(RELATIONS, line, "from_ref", record["from_ref"])
        self._issue_scope(RELATIONS, line, "to_ref", record["to_ref"])
        if record["from_ref"] == record["to_ref"]:
            self.report.error(ProblemCode.INVALID_REF, RELATIONS.name, "An issue cannot relate to itself", line=line)
        if project_ref is not None:
            self._in_project(RELATIONS, project_ref, offset)

    def _check_wiki_pages(self, line: int, offset: int, record: dict[str, Any]) -> None:
        name = WIKI_PAGES.name
        ref = record["source_ref"]
        project_ref = self._scoped(WIKI_PAGES, line, offset, record)
        self.page_project.setdefault(ref, project_ref)
        self.page_nodes.setdefault(project_ref, {}).setdefault(ref, (record.get("parent_ref"), offset))
        self._ref(name, line, "parent_ref", EntityType.WIKI_PAGE, record.get("parent_ref"))

        previous = 0
        for version in record.get("versions") or []:
            self._register(name, line, EntityType.WIKI_VERSION, version["source_ref"])
            self.report.records["wiki_versions"] += 1
            if version["version"] <= previous:
                self.report.error(
                    ProblemCode.SCHEMA,
                    name,
                    "versions must be ordered oldest first, with increasing version numbers",
                    line=line,
                )
            previous = version["version"]
            self._ref(name, line, "versions.author_ref", EntityType.USER, version.get("author_ref"))
            self._text(name, line, "versions.text", version.get("text"))

    def _check_wiki_redirects(self, line: int, offset: int, record: dict[str, Any]) -> None:
        project_ref = self._scoped(WIKI_REDIRECTS, line, offset, record)
        self._in_project(WIKI_REDIRECTS, project_ref, offset)
        key = (project_ref, record["title"])
        if key in self.redirects:
            self.report.error(
                ProblemCode.DUPLICATE_REF,
                WIKI_REDIRECTS.name,
                f"Project {project_ref!r} already has a redirect from {record['title']!r}",
                line=line,
            )
        self.redirects.add(key)

    def _check_watchers(self, line: int, offset: int, record: dict[str, Any]) -> None:
        name = WATCHERS.name
        self._ref(name, line, "user_ref", EntityType.USER, record["user_ref"])
        container = record["container_ref"]
        key = (record["container_kind"], container, record["user_ref"])
        if key in self.watchers:
            self.report.warning(ProblemCode.DUPLICATE_REF, name, "The same watcher is listed twice", line=line)
            return
        self.watchers.add(key)

        if record["container_kind"] == ContainerKind.ISSUE:
            project_ref = self._issue_scope(WATCHERS, line, "container_ref", container)
            target = self.index.issue_watchers
        else:
            self._ref(name, line, "container_ref", EntityType.WIKI_PAGE, container)
            project_ref = self.page_project.get(container)
            target = self.index.wiki_watchers
        if project_ref is not None:
            target.setdefault(project_ref, []).append(offset)

    def _check_attachments(self, line: int, offset: int, record: dict[str, Any]) -> None:
        name = ATTACHMENTS.name
        container = record["container_ref"]
        if record["container_kind"] == ContainerKind.ISSUE:
            project_ref = self._issue_scope(ATTACHMENTS, line, "container_ref", container)
        else:
            self._ref(name, line, "container_ref", EntityType.WIKI_PAGE, container)
            project_ref = self.page_project.get(container)
        if project_ref is not None:
            self._in_project(ATTACHMENTS, project_ref, offset)
        self._ref(name, line, "author_ref", EntityType.USER, record.get("author_ref"))

        sha256 = record["sha256"]
        size = self._stored_file(sha256)
        if size is None:
            self.report.error(ProblemCode.MISSING_FILE, name, f"files/{sha256} is not in the bundle", line=line)
        elif size < 0:
            self.report.error(ProblemCode.HASH_MISMATCH, name, f"files/{sha256} does not hash to its name", line=line)
        elif record.get("filesize") is not None and record["filesize"] != size:
            self.report.warning(
                ProblemCode.SIZE_MISMATCH,
                name,
                f"filesize says {record['filesize']} bytes but files/{sha256} holds {size}; the real size is kept",
                line=line,
            )

    def _stored_file(self, sha256: str) -> int | None:
        """Return the size of ``files/<sha256>``, ``None`` if absent, or -1 if its bytes do not match.

        Each file is hashed once, however many attachments share it.
        """
        if sha256 not in self.stored_files:
            path = self.root / FILES_DIR / sha256
            if not path.is_file() or path.is_symlink():
                self.stored_files[sha256] = None
            else:
                digest = hashlib.sha256()
                size = 0
                with path.open("rb") as handle:
                    while chunk := handle.read(_HASH_CHUNK):
                        digest.update(chunk)
                        size += len(chunk)
                self.stored_files[sha256] = size if digest.hexdigest() == sha256 else -1
        return self.stored_files[sha256]

    def _check_time_entries(self, line: int, offset: int, record: dict[str, Any]) -> None:
        name = TIME_ENTRIES.name
        self._in_project(TIME_ENTRIES, self._scoped(TIME_ENTRIES, line, offset, record), offset)
        self._ref(name, line, "issue_ref", EntityType.ISSUE, record.get("issue_ref"))
        self._ref(name, line, "user_ref", EntityType.USER, record.get("user_ref"))
        self._ref(name, line, "activity_ref", EntityType.ACTIVITY, record.get("activity_ref"))

    # -- the end -----------------------------------------------------------

    def _finish(self) -> None:
        for pending in self.pending:
            self._resolve(pending)

        order, cyclic = order_parents_first(self.project_parents)
        self._report_cycles(PROJECTS, cyclic)
        self.index.projects = [self.project_offsets[ref] for ref in order + cyclic]

        self._order_tree(ISSUES, self.issue_nodes)
        self._order_tree(WIKI_PAGES, self.page_nodes)
        self._check_page_parents()

        journals = self.index.by_project.setdefault(JOURNALS.name, {})
        for project_ref, keys in self.journal_keys.items():
            # Oldest first, ties in file order: the loader numbers each issue's
            # history in the order it is given.
            journals[project_ref] = [offset for _, _, offset in sorted(keys)]

        self._report_unreferenced_files()
        self._check_counts()

    def _order_tree(self, record_file: RecordFile, nodes: dict[str, dict[str, tuple[str | None, int]]]) -> None:
        ordered = self.index.by_project.setdefault(record_file.name, {})
        for project_ref, members in nodes.items():
            order, cyclic = order_parents_first({ref: parent for ref, (parent, _) in members.items()})
            self._report_cycles(record_file, cyclic)
            ordered[project_ref] = [members[ref][1] for ref in order + cyclic]

    def _report_cycles(self, record_file: RecordFile, cyclic: list[str]) -> None:
        if cyclic:
            refs = ", ".join(repr(ref) for ref in cyclic[:10])
            self.report.error(
                ProblemCode.PARENT_CYCLE,
                record_file.name,
                f"parent_ref forms a cycle, so these cannot be ordered parents first: {refs}",
            )

    def _check_page_parents(self) -> None:
        for project_ref, members in self.page_nodes.items():
            for ref, (parent, _) in members.items():
                if parent is not None and parent in self.page_project and self.page_project[parent] != project_ref:
                    self.report.error(
                        ProblemCode.INVALID_REF,
                        WIKI_PAGES.name,
                        f"Wiki page {ref!r} has a parent in another project",
                        line=self.refs[EntityType.WIKI_PAGE].get(ref),
                    )

    def _report_unreferenced_files(self) -> None:
        files_dir = self.root / FILES_DIR
        if not files_dir.is_dir():
            return
        for path in sorted(files_dir.iterdir()):
            if SHA256_RE.match(path.name) and path.name not in self.stored_files:
                self.report.warning(
                    ProblemCode.UNREFERENCED_FILE,
                    f"{FILES_DIR}/{path.name}",
                    "No attachment refers to this file; it is not imported",
                )


def _parse_version(value: Any) -> tuple[int | None, int]:
    """Return ``(major, minor)`` of a ``MAJOR.MINOR`` string, or ``(None, 0)``."""
    if not isinstance(value, str):
        return None, 0
    major, _, minor = value.partition(".")
    if not major.isdigit() or not minor.isdigit():
        return None, 0
    return int(major), int(minor)
