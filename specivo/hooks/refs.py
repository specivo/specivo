"""Issue references in commit messages and branch names.

Pure functions, no database: the caller passes ``known_keys``, the project
keys an integration may link to (its scope). A reference whose key is not in
that set is not a reference at all, so ``HTTP-200`` in a message links nothing
unless ``HTTP`` is an in-scope key, and an issue in another project is never
touched or revealed.

Messages: a bare ``KEY-123`` (key upper case, as projects store it), with an
optional leading ``#``, not glued to a word, a path or a URL. The keyword just
before it says what the commit claims:

- ``fixes``, ``fixed``, ``fix``, ``closes``, ``closed``, ``close``,
  ``resolves``, ``resolved``, ``resolve`` → ``fixes``;
- ``refs``, ``references``, ``ref``, ``see``, or no keyword → ``refs``.

A list after one keyword (``Fixes ACME-1, ACME-2 and ACME-3``) shares it.

Branches: ``KEY-123`` anywhere in the name as its own token (``KEY-123``,
``feat/KEY-123-slug``, ``KEY-123/slug``), and lower case is accepted, since
branch names are often lower-cased (``key-123-slug``). Branch references are
always ``refs``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

Keyword = Literal["refs", "fixes"]

_FIXING_WORDS = ("fixes", "fixed", "fix", "closes", "closed", "close", "resolves", "resolved", "resolve")
_REFERENCING_WORDS = ("refs", "references", "ref", "see")

# Not preceded by a word character, ``#`` (``x#KEY-1``), or anything that makes
# it part of a path or URL (``/``, ``.``, ``-``).
# Issue numbers have at most 9 digits: they fit the database column and ``int()``
# never sees an attacker's thousand-digit number.
_MESSAGE_REF = re.compile(r"(?<![\w/.\-#])#?(?P<key>[A-Z][A-Z0-9]*)-(?P<number>\d{1,9})(?!\w)")
_KEYWORD_BEFORE = re.compile(
    r"\b(?P<word>"
    + "|".join(_FIXING_WORDS + _REFERENCING_WORDS)
    + r")\b[ \t]*(?::[ \t]*)?$",  # one way to split the blanks: linear on long gaps
    re.IGNORECASE,
)
# What may stand between two references that share a keyword (same line only).
_LIST_SEPARATOR = re.compile(r"(?:[ \t]|,|&|\band\b)*", re.IGNORECASE)
_BRANCH_REF = re.compile(r"(?<![A-Za-z0-9])(?P<key>[A-Za-z][A-Za-z0-9]*)-(?P<number>\d{1,9})(?!\d)")


@dataclass(frozen=True)
class Ref:
    """One issue reference: project key, issue number, and what the commit claims."""

    key: str
    number: int
    keyword: Keyword = "refs"

    @property
    def display_key(self) -> str:
        return f"{self.key}-{self.number}"


def _keyword_for(gap: str, previous: Keyword | None) -> Keyword:
    match = _KEYWORD_BEFORE.search(gap)
    if match is not None:
        return "fixes" if match.group("word").lower() in _FIXING_WORDS else "refs"
    if previous is not None and _LIST_SEPARATOR.fullmatch(gap):
        return previous
    return "refs"


def _dedupe(refs: Iterable[Ref]) -> list[Ref]:
    """One ref per issue, in order of first mention; ``fixes`` wins over ``refs``."""
    seen: dict[str, Ref] = {}
    for ref in refs:
        current = seen.get(ref.display_key)
        if current is None or (current.keyword == "refs" and ref.keyword == "fixes"):
            seen[ref.display_key] = Ref(ref.key, ref.number, ref.keyword if current is None else "fixes")
    return list(seen.values())


def extract_refs(text: str, known_keys: frozenset[str] | set[str]) -> list[Ref]:
    """Return the in-scope issue references in a commit message, one per issue."""
    if not text or not known_keys:
        return []
    found: list[Ref] = []
    previous: Keyword | None = None
    last_end = 0
    for match in _MESSAGE_REF.finditer(text):
        number = int(match.group("number"))
        if match.group("key") not in known_keys or number == 0:
            continue
        keyword = _keyword_for(text[last_end : match.start()], previous)
        found.append(Ref(match.group("key"), number, keyword))
        previous = keyword
        last_end = match.end()
    return _dedupe(found)


def extract_branch_refs(branch: str, known_keys: frozenset[str] | set[str]) -> list[Ref]:
    """Return the in-scope issue references in a branch name, one per issue."""
    if not branch or not known_keys:
        return []
    found: list[Ref] = []
    for match in _BRANCH_REF.finditer(branch):
        key = match.group("key").upper()
        number = int(match.group("number"))
        if key in known_keys and number > 0:
            found.append(Ref(key, number))
    return _dedupe(found)
