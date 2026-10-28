"""The validation report: every problem found in a bundle, with where it is.

A problem names the file and, for a JSON-lines file, the line (1-based); for
``manifest.json``, ``lookups.json`` and ``public_access.json`` it gives a JSON
pointer instead. Errors make the bundle unloadable; warnings are reported and
loading goes ahead.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# A badly broken bundle can produce a problem per line. The report keeps the
# first ones in full and only counts the rest, so it stays readable and small.
MAX_PROBLEMS = 1000


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


class ProblemCode(StrEnum):
    """What kind of problem was found. Stable: tools may match on these."""

    UNREADABLE = "unreadable"
    UNSAFE_ENTRY = "unsafe_entry"
    TOO_LARGE = "too_large"
    SYMLINK = "symlink"
    UNKNOWN_FILE = "unknown_file"
    UNSUPPORTED_VERSION = "unsupported_version"
    INVALID_JSON = "invalid_json"
    SCHEMA = "schema"
    DUPLICATE_REF = "duplicate_ref"
    UNRESOLVED_REF = "unresolved_ref"
    INVALID_REF = "invalid_ref"
    EXTERNAL_REF = "external_ref"
    PARENT_CYCLE = "parent_cycle"
    MISSING_FILE = "missing_file"
    HASH_MISMATCH = "hash_mismatch"
    SIZE_MISMATCH = "size_mismatch"
    UNREFERENCED_FILE = "unreferenced_file"
    COUNT_MISMATCH = "count_mismatch"


@dataclass(frozen=True, slots=True)
class Problem:
    severity: Severity
    code: ProblemCode
    file: str
    message: str
    line: int | None = None
    pointer: str | None = None

    @property
    def location(self) -> str:
        if self.line is not None:
            return f"{self.file}:{self.line}"
        if self.pointer:
            return f"{self.file}#{self.pointer}"
        return self.file

    def format(self) -> str:
        return f"{self.location}: {self.severity}: {self.message} [{self.code}]"

    def as_dict(self) -> dict[str, Any]:
        return {
            "severity": str(self.severity),
            "code": str(self.code),
            "file": self.file,
            "line": self.line,
            "pointer": self.pointer,
            "message": self.message,
        }


@dataclass
class ValidationReport:
    """Problems found in one bundle, and how many records each file held."""

    path: str
    problems: list[Problem] = field(default_factory=list)
    totals: Counter[Severity] = field(default_factory=Counter)
    records: Counter[str] = field(default_factory=Counter)

    def add(
        self,
        severity: Severity,
        code: ProblemCode,
        file: str,
        message: str,
        *,
        line: int | None = None,
        pointer: str | None = None,
    ) -> None:
        self.totals[severity] += 1
        if len(self.problems) < MAX_PROBLEMS:
            self.problems.append(Problem(severity, code, file, message, line, pointer))

    def error(self, code: ProblemCode, file: str, message: str, **where: Any) -> None:
        self.add(Severity.ERROR, code, file, message, **where)

    def warning(self, code: ProblemCode, file: str, message: str, **where: Any) -> None:
        self.add(Severity.WARNING, code, file, message, **where)

    @property
    def ok(self) -> bool:
        """Whether the bundle can be loaded: it has no errors."""
        return self.totals[Severity.ERROR] == 0

    @property
    def errors(self) -> list[Problem]:
        return [p for p in self.problems if p.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[Problem]:
        return [p for p in self.problems if p.severity is Severity.WARNING]

    def format_text(self, limit: int | None = None) -> str:
        """Return the report as printed by ``--validate``, at most *limit* problems."""
        verdict = "valid" if self.ok else "INVALID"
        lines = [
            f"Bundle {self.path}: {verdict} — "
            f"{self.totals[Severity.ERROR]} error(s), {self.totals[Severity.WARNING]} warning(s)"
        ]
        if self.records:
            lines.append("")
            lines.append("Records:")
            lines.extend(f"  {name:<20} {count}" for name, count in sorted(self.records.items()))
        shown = self.problems if limit is None else self.problems[:limit]
        if shown:
            lines.append("")
            lines.extend(problem.format() for problem in shown)
        hidden = sum(self.totals.values()) - len(shown)
        if hidden > 0:
            lines.append(f"... and {hidden} more")
        return "\n".join(lines)

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "valid": self.ok,
            "errors": self.totals[Severity.ERROR],
            "warnings": self.totals[Severity.WARNING],
            "records": dict(sorted(self.records.items())),
            "problems": [problem.as_dict() for problem in self.problems],
        }
