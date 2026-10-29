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
A placeholder whose record was not imported becomes its label, or else its
ref, as plain text: a reader never sees the placeholder syntax.
Text inside fenced code blocks is left alone, so a code sample can show a
placeholder without it being expanded. Fences pair the way CommonMark pairs
them (see :func:`_fenced_blocks`); inline code spans are not code here.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import StrEnum

from specivo.importers.core.ir import EntityType


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

# A line that may open or close a fenced code block: up to three spaces, a run
# of three or more backticks or tildes, then the rest of the line.
_FENCE_LINE_RE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)")


@dataclass(frozen=True, slots=True)
class Placeholder:
    """One placeholder found in a text."""

    kind: PlaceholderKind
    ref: str
    label: str | None = None

    @property
    def entity_type(self) -> EntityType:
        return PLACEHOLDER_ENTITY_TYPES[self.kind]


def _fenced_blocks(text: str) -> Iterator[tuple[int, int]]:
    """Yield the ``(start, end)`` offsets of each fenced code block in *text*.

    Fences pair as in CommonMark: a fence is a whole line of three or more
    backticks or tildes, indented by at most three spaces (a backtick fence's
    info string holds no backtick). The block ends at a line of the same
    character, at least as long, with nothing after it but spaces and tabs,
    or else at the end of the text. A block starts at the beginning of its
    opening line and ends after its closing line.
    """
    opening = ""
    block_start = offset = 0
    for line in text.split("\n"):
        line_end = offset + len(line)
        match = _FENCE_LINE_RE.fullmatch(line.rstrip("\r"))
        if match is not None:
            run, rest = match.groups()
            if not opening:
                if not (run[0] == "`" and "`" in rest):
                    opening, block_start = run, offset
            elif run[0] == opening[0] and len(run) >= len(opening) and not rest.strip(" \t"):
                yield block_start, line_end
                opening = ""
        offset = line_end + 1
    if opening:
        yield block_start, len(text)


def _outside_code(text: str) -> Iterator[tuple[int, int]]:
    """Yield the ``(start, end)`` offsets of each stretch of *text* outside fenced code blocks."""
    position = 0
    for start, end in _fenced_blocks(text):
        yield position, start
        position = end
    yield position, len(text)


def _placeholder(match: re.Match[str]) -> Placeholder:
    label = match.group(3)
    return Placeholder(PlaceholderKind(match.group(1)), match.group(2), label if label else None)


def find_placeholders(text: str | None) -> Iterator[Placeholder]:
    """Yield every placeholder in *text*, outside fenced code blocks."""
    if not text or "{{" not in text:
        return
    for start, end in _outside_code(text):
        for match in PLACEHOLDER_RE.finditer(text, start, end):
            yield _placeholder(match)


def expand_placeholders(text: str, resolve: Callable[[Placeholder], str | None]) -> tuple[str, list[Placeholder]]:
    """Return *text* with each placeholder replaced by what *resolve* returns.

    A placeholder *resolve* cannot place (it returns ``None``) is written as
    its label, or as its ref when it has none, as plain text, so a reader never
    sees the placeholder syntax. Those are returned alongside the text.
    """
    unresolved: list[Placeholder] = []
    if "{{" not in text:
        return text, unresolved

    def replace(match: re.Match[str]) -> str:
        placeholder = _placeholder(match)
        expanded = resolve(placeholder)
        if expanded is None:
            unresolved.append(placeholder)
            return placeholder.label or placeholder.ref
        return expanded

    parts: list[str] = []
    previous = 0
    for start, end in _outside_code(text):
        parts.append(text[previous:start])
        parts.append(PLACEHOLDER_RE.sub(replace, text[start:end]))
        previous = end
    return "".join(parts), unresolved
