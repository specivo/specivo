"""Branch names for issues: the "Copy branch name" button on the issue page.

The server builds the name from the project's format, so there is one source
of truth and the button only copies text. The default format is
``{type}/{key}-{slug}``:

- ``type`` comes from the tracker name (``Bug`` → ``fix``, ``Feature`` →
  ``feat``, anything else → ``chore``), configurable per project;
- ``key`` is the issue's display key (``ACME-12``);
- ``slug`` is the subject lower-cased and folded to ASCII, with every run of
  other characters turned into ``-``, at most 40 characters.

A subject with no Latin letters or digits (Thai, for example) has an empty
slug; the separators around it are dropped, so the default format gives
``fix/ACME-12``. Every name is a valid git branch name, and
``specivo.hooks.refs.extract_branch_refs`` finds the issue key in it again.

Settings live in ``projects.settings["branch_name"]``:
``{"format": ..., "types": {tracker name: type}, "default_type": ...}``.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

#: Key of the branch name settings in ``projects.settings``.
BRANCH_NAME_SETTINGS_KEY = "branch_name"
DEFAULT_BRANCH_FORMAT = "{type}/{key}-{slug}"
DEFAULT_BRANCH_TYPES: dict[str, str] = {"bug": "fix", "feature": "feat"}
DEFAULT_BRANCH_TYPE = "chore"
SLUG_MAX_LENGTH = 40
FORMAT_MAX_LENGTH = 100
TYPE_MAX_LENGTH = 30

_PLACEHOLDER = re.compile(r"\{([a-z]+)\}")
_PLACEHOLDERS = frozenset({"type", "key", "slug"})
_KEY_PLACEHOLDER = re.compile(r"\{key\}")
_KEY_SEPARATORS = "._/-"
# What the literal text of a format may hold: characters every git ref accepts.
_LITERAL = re.compile(r"[A-Za-z0-9._/-]*")
_NOT_SLUG = re.compile(r"[^a-z0-9]+")
_SEPARATOR_RUN = re.compile(r"([-_.])[-_.]+")
_EDGE_SEPARATORS = "-_."


def slugify(text: str, max_length: int = SLUG_MAX_LENGTH) -> str:
    """Lower-case ASCII words joined by ``-``, at most *max_length* characters, no ``-`` at either end."""
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    return _NOT_SLUG.sub("-", folded).strip("-")[:max_length].rstrip("-")


def validate_branch_format(fmt: str) -> str:
    """Return *fmt* when it is a usable format; ``ValueError`` saying why otherwise.

    It must contain ``{key}``, use only ``{type}``, ``{key}`` and ``{slug}``,
    and its other characters must be letters, digits, ``.``, ``_``, ``-`` or
    ``/``, with no ``..``, ``//`` or ``/`` at either end. ``{key}`` must stand
    apart: at either end or next to ``.``, ``_``, ``-`` or ``/``. Glued to a
    letter, digit or placeholder (``{key}{slug}`` with the subject "2fa"
    gives ``ACME-122fa``), the branch parser would miss it or read another
    issue.
    """
    if not fmt or len(fmt) > FORMAT_MAX_LENGTH:
        raise ValueError(f"The format must be 1 to {FORMAT_MAX_LENGTH} characters")
    names = _PLACEHOLDER.findall(fmt)
    if "key" not in names:
        raise ValueError("The format must contain {key}")
    unknown = sorted(set(names) - _PLACEHOLDERS)
    if unknown:
        raise ValueError("Unknown placeholder: {" + unknown[0] + "}")
    literal = _PLACEHOLDER.sub("", fmt)
    if not _LITERAL.fullmatch(literal):
        raise ValueError("The format may only hold letters, digits, '.', '_', '-', '/' and placeholders")
    if ".." in fmt or "//" in fmt or fmt.startswith("/") or fmt.endswith("/"):
        raise ValueError("The format must not contain '..' or '//', or start or end with '/'")
    for match in _KEY_PLACEHOLDER.finditer(fmt):
        before = fmt[match.start() - 1] if match.start() > 0 else ""
        after = fmt[match.end()] if match.end() < len(fmt) else ""
        if (before and before not in _KEY_SEPARATORS) or (after and after not in _KEY_SEPARATORS):
            raise ValueError("{key} must be separated from the rest by '.', '_', '-' or '/'")
    return fmt


def _ref_component(part: str) -> str:
    """One ``/``-separated part of a branch name made safe for git."""
    part = _SEPARATOR_RUN.sub(r"\1", part).strip(_EDGE_SEPARATORS)
    while part.endswith(".lock"):
        part = part.removesuffix(".lock").rstrip(_EDGE_SEPARATORS)
    return part


@dataclass(frozen=True)
class BranchNameSettings:
    """A project's branch name format and tracker → type mapping."""

    format: str = DEFAULT_BRANCH_FORMAT
    types: Mapping[str, str] = field(default_factory=lambda: dict(DEFAULT_BRANCH_TYPES))
    default_type: str = DEFAULT_BRANCH_TYPE

    def __post_init__(self) -> None:
        # Tracker names compare case-insensitively.
        object.__setattr__(self, "types", {k.strip().lower(): v for k, v in self.types.items()})

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, BranchNameSettings):
            return NotImplemented
        return (self.format, dict(self.types), self.default_type) == (
            other.format,
            dict(other.types),
            other.default_type,
        )

    def __hash__(self) -> int:
        return hash((self.format, tuple(sorted(self.types.items())), self.default_type))

    def type_for(self, tracker_name: str) -> str:
        return self.types.get(tracker_name.strip().lower(), self.default_type)

    @classmethod
    def from_project_settings(cls, project_settings: Mapping[str, Any] | None) -> BranchNameSettings:
        """The settings stored on a project; defaults for anything missing or unusable."""
        raw = (project_settings or {}).get(BRANCH_NAME_SETTINGS_KEY)
        if not isinstance(raw, Mapping):
            return cls()
        fmt = raw.get("format")
        try:
            fmt = validate_branch_format(fmt) if isinstance(fmt, str) else DEFAULT_BRANCH_FORMAT
        except ValueError:
            fmt = DEFAULT_BRANCH_FORMAT
        types = raw.get("types")
        if not isinstance(types, Mapping) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in types.items()
        ):
            types = DEFAULT_BRANCH_TYPES
        default_type = raw.get("default_type")
        if not isinstance(default_type, str):
            default_type = DEFAULT_BRANCH_TYPE
        return cls(format=fmt, types=dict(types), default_type=default_type)

    def to_project_settings(self) -> dict[str, Any]:
        return {"format": self.format, "types": dict(self.types), "default_type": self.default_type}


def make_branch_name(settings: BranchNameSettings, *, tracker_name: str, display_key: str, subject: str) -> str:
    """The branch name for an issue, a valid git branch name that contains *display_key*."""
    values = {
        "type": slugify(settings.type_for(tracker_name), TYPE_MAX_LENGTH),
        "key": display_key,
        "slug": slugify(subject),
    }
    rendered = _PLACEHOLDER.sub(lambda m: values.get(m.group(1), ""), settings.format)
    parts = [_ref_component(part) for part in rendered.split("/")]
    name = "/".join(part for part in parts if part)
    # The key is never empty and holds only letters, digits and "-", so this is
    # a fallback for a format the validator should have refused.
    return name or display_key
