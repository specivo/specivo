"""Placeholders for references inside imported text.

A source that writes Markdown cannot know what an issue, a user, an attachment
or a wiki page will be called in Specivo: project keys are assigned by the
import, a login can be suffixed on a clash, a file is renamed on upload. So
text names the record by its source ref instead, and a pass after loading
replaces each placeholder with the Specivo form:

==========================  ===============================================
``{{issue:REF}}``           the issue's display key, ``ACME-15``
``{{issue:REF|label}}``     a link to the issue reading *label*
``{{user:REF}}``            a mention, ``@login``
``{{attachment:REF}}``      the file name, as a Markdown link target
``{{wiki:REF}}``            a link to the page, ``[[Title]]``
``{{wiki:REF|label}}``      a link to the page reading *label*
==========================  ===============================================

Only those four kinds are recognised; any other ``{{...}}`` is ordinary text.
Text inside fenced code blocks is left alone, as the Redmine reference rewrite
does, so a code sample can show a placeholder without it being expanded.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import StrEnum

from specivo.importers.core.ir import EntityType

# Values of an adapter's ``reference_style``: how its text refers to other
# records. Redmine writes ``#123``; a bundle writes placeholders.
REFERENCE_STYLE_HASH = "hash"
REFERENCE_STYLE_PLACEHOLDERS = "placeholders"


class PlaceholderKind(StrEnum):
    """What a placeholder refers to."""

    ISSUE = "issue"
    USER = "user"
    ATTACHMENT = "attachment"
    WIKI = "wiki"


# The id-map entity type each kind's ref is looked up under.
PLACEHOLDER_ENTITY_TYPES: dict[PlaceholderKind, EntityType] = {
    PlaceholderKind.ISSUE: EntityType.ISSUE,
    PlaceholderKind.USER: EntityType.USER,
    PlaceholderKind.ATTACHMENT: EntityType.ATTACHMENT,
    PlaceholderKind.WIKI: EntityType.WIKI_PAGE,
}

# Kinds that may carry a ``|label``. A mention and a link target have no text
# of their own to replace.
LABELLED_KINDS = frozenset({PlaceholderKind.ISSUE, PlaceholderKind.WIKI})

# A ref is a source_ref, which the bundle format keeps free of whitespace,
# braces and the bar, so the grammar needs no escaping.
PLACEHOLDER_RE = re.compile(r"\{\{(issue|user|attachment|wiki):([^\s{}|]+)(?:\|([^{}\n]*))?\}\}")

# Splits text on fenced code blocks, keeping the fences so they can be rejoined.
_FENCE_RE = re.compile(r"(```.*?```|~~~.*?~~~)", re.DOTALL)


@dataclass(frozen=True, slots=True)
class Placeholder:
    """One placeholder found in a text."""

    kind: PlaceholderKind
    ref: str
    label: str | None = None

    @property
    def entity_type(self) -> EntityType:
        return PLACEHOLDER_ENTITY_TYPES[self.kind]


def _outside_code(text: str) -> Iterator[tuple[int, str]]:
    """Yield ``(index, part)`` for each part of *text* that is not a fenced block."""
    for index, part in enumerate(_FENCE_RE.split(text)):
        # Odd positions are the fenced blocks themselves.
        if index % 2 == 0:
            yield index, part


def _placeholder(match: re.Match[str]) -> Placeholder:
    label = match.group(3)
    return Placeholder(PlaceholderKind(match.group(1)), match.group(2), label if label else None)


def find_placeholders(text: str | None) -> Iterator[Placeholder]:
    """Yield every placeholder in *text*, outside fenced code blocks."""
    if not text or "{{" not in text:
        return
    for _, part in _outside_code(text):
        for match in PLACEHOLDER_RE.finditer(part):
            yield _placeholder(match)


def expand_placeholders(text: str, resolve: Callable[[Placeholder], str | None]) -> tuple[str, list[Placeholder]]:
    """Return *text* with each placeholder replaced by what *resolve* returns.

    A placeholder *resolve* cannot place (it returns ``None``) is kept exactly
    as written, so a later run that imports the missing record can still
    expand it. Those are returned alongside the text.
    """
    unresolved: list[Placeholder] = []
    if "{{" not in text:
        return text, unresolved

    def replace(match: re.Match[str]) -> str:
        placeholder = _placeholder(match)
        expanded = resolve(placeholder)
        if expanded is None:
            unresolved.append(placeholder)
            return match.group(0)
        return expanded

    parts = _FENCE_RE.split(text)
    for index, part in _outside_code(text):
        parts[index] = PLACEHOLDER_RE.sub(replace, part)
    return "".join(parts), unresolved
