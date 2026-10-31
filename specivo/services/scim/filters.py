"""The SCIM ``filter`` query parameter, restricted to what identity providers send.

Supported (RFC 7644 section 3.4.2.2 subset): equality comparisons joined with
``and``::

    userName eq "alice@example.com"
    externalId eq "0a21f0f2-..."
    emails[type eq "work"].value eq "alice@example.com"     (also emails.value)
    displayName eq "Engineering"                            (groups)
    id eq "..."

Attribute names and ``eq`` / ``and`` are case-insensitive, an attribute may
carry its schema URN, and the value is a JSON string literal. Everything else
(other operators, ``or``, ``not``, grouping, other attributes, non-string
values) is refused with 400 ``invalidFilter``: the text is matched against a
fixed grammar and only the resulting :class:`Comparison` values reach a query,
as bound parameters.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal

from specivo.services.scim.errors import ScimError

__all__ = ["MAX_FILTER_LENGTH", "Comparison", "parse_filter"]

ResourceType = Literal["User", "Group"]

#: Longer filters are refused before parsing.
MAX_FILTER_LENGTH = 1000

_CORE_USER_URN = "urn:ietf:params:scim:schemas:core:2.0:user:"
_CORE_GROUP_URN = "urn:ietf:params:scim:schemas:core:2.0:group:"

# attrPath: an optional schema URN, a name, an optional [type eq "work"]
# value filter, an optional .sub attribute. Then "eq" and a JSON string.
_COMPARISON = re.compile(
    r"""\s*
    (?P<attr>(?:urn:[A-Za-z0-9.:-]+:)?[A-Za-z][A-Za-z0-9_-]*)
    (?:\[\s*(?P<fattr>[A-Za-z][A-Za-z0-9_-]*)\s+(?P<fop>[A-Za-z]+)\s+(?P<fvalue>"(?:[^"\\]|\\.)*")\s*\])?
    (?:\.(?P<sub>[A-Za-z][A-Za-z0-9_-]*))?
    \s+(?P<op>[A-Za-z]+)\s+
    (?P<value>"(?:[^"\\]|\\.)*")
    \s*""",
    re.VERBOSE,
)
_AND = re.compile(r"and\s", re.IGNORECASE)

_USER_ATTRIBUTES = {"username": "userName", "externalid": "externalId", "id": "id"}
_GROUP_ATTRIBUTES = {"displayname": "displayName", "externalid": "externalId", "id": "id"}


@dataclass(frozen=True)
class Comparison:
    """``attribute eq value``; ``attribute`` is canonical (``userName``, ``emails.value`` ...)."""

    attribute: str
    value: str


def _string(literal: str) -> str:
    try:
        value = json.loads(literal)
    except ValueError:
        raise ScimError.invalid_filter("The filter value is not a valid string.") from None
    if not isinstance(value, str):  # pragma: no cover - the regex only admits string literals
        raise ScimError.invalid_filter("The filter value must be a string.")
    return value


def _strip_urn(name: str, resource_type: ResourceType) -> str:
    lowered = name.lower()
    prefix = _CORE_USER_URN if resource_type == "User" else _CORE_GROUP_URN
    if lowered.startswith(prefix):
        return lowered[len(prefix) :]
    if lowered.startswith("urn:"):
        raise ScimError.invalid_filter("Filtering on this attribute is not supported.")
    return lowered


def _attribute(match: re.Match[str], resource_type: ResourceType) -> str:
    name = _strip_urn(match.group("attr"), resource_type)
    sub = match.group("sub")
    has_filter = match.group("fattr") is not None

    if resource_type == "User" and name == "emails":
        if sub is None or sub.lower() != "value":
            raise ScimError.invalid_filter("Only emails.value can be filtered on.")
        if has_filter:
            if (
                match.group("fattr").lower() != "type"
                or match.group("fop").lower() != "eq"
                or _string(match.group("fvalue")).lower() != "work"
            ):
                raise ScimError.invalid_filter('Only emails[type eq "work"].value can be filtered on.')
        return "emails.value"

    if has_filter or sub is not None:
        raise ScimError.invalid_filter("Filtering on this attribute is not supported.")
    table = _USER_ATTRIBUTES if resource_type == "User" else _GROUP_ATTRIBUTES
    canonical = table.get(name)
    if canonical is None:
        raise ScimError.invalid_filter("Filtering on this attribute is not supported.")
    return canonical


def parse_filter(text: str, resource_type: ResourceType) -> list[Comparison]:
    """The comparisons of ``text`` (all must hold), or :class:`ScimError` ``invalidFilter``."""
    if not text or not text.strip():
        raise ScimError.invalid_filter("The filter is empty.")
    if len(text) > MAX_FILTER_LENGTH:
        raise ScimError.invalid_filter("The filter is too long.")

    comparisons: list[Comparison] = []
    pos = 0
    while True:
        match = _COMPARISON.match(text, pos)
        if match is None:
            raise ScimError.invalid_filter("Only eq comparisons joined with and are supported.")
        if match.group("op").lower() != "eq":
            raise ScimError.invalid_filter("Only the eq operator is supported.")
        comparisons.append(Comparison(_attribute(match, resource_type), _string(match.group("value"))))
        pos = match.end()
        if pos == len(text):
            return comparisons
        joiner = _AND.match(text, pos)
        if joiner is None:
            raise ScimError.invalid_filter("Only eq comparisons joined with and are supported.")
        pos = joiner.end()
