"""Parsing and query-string building for the issue list filters.

One place for the rules the issue list page, the board view, the htmx table
partial, saved filters and the issues API share:

- ``assigned_to_id`` takes a user id, ``"me"`` (the reader) or ``"none"``
  (unassigned issues);
- ``fixed_version_id`` takes a version id or ``"none"`` (issues without a
  target version). The API also accepts the older name ``version_id``;
- ``tag_id`` takes a tag id.

``IssueService.list_issues`` understands the ``"none"`` sentinel
(:data:`NO_VALUE`) for ``assigned_to_id`` and ``version_id``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal
from urllib.parse import urlencode

NO_VALUE: Literal["none"] = "none"
ME = "me"

# Query parameters of the issue list page that describe *what* is listed, in
# the order they are written back into links. ``offset`` is never kept: a link
# built from these starts at the first page.
LIST_FILTER_PARAMS: tuple[str, ...] = (
    "status",
    "tracker_id",
    "assigned_to_id",
    "priority_id",
    "tag_id",
    "fixed_version_id",
    "sort",
    "limit",
    "view",
    "board_per_col",
)

# Values that are the page's own defaults and so are left out of links.
_DEFAULTS: dict[str, str] = {
    "status": "open",
    "sort": "created_at:desc",
    "limit": "25",
    "view": "list",
    "board_per_col": "10",
}

# Saved filter definitions may spell the target version either way.
_SAVED_FILTER_ALIASES: dict[str, str] = {"version_id": "fixed_version_id"}


def parse_id(raw: object) -> int | None:
    """Return *raw* as a positive integer id, or None."""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def is_no_value(raw: object) -> bool:
    """True when *raw* is the ``"none"`` sentinel (any case)."""
    return isinstance(raw, str) and raw.strip().lower() == NO_VALUE


def parse_assignee(raw: object, *, me_id: int | None) -> int | Literal["none"] | None:
    """Resolve an ``assigned_to_id`` filter value.

    ``"none"`` selects unassigned issues; ``"me"`` the reader, or nothing when
    there is no reader to stand for (``me_id`` None, the anonymous visitor);
    anything else must be a user id. Unparseable values select nothing.
    """
    if is_no_value(raw):
        return NO_VALUE
    if isinstance(raw, str) and raw.strip().lower() == ME:
        return me_id
    return parse_id(raw)


def parse_version(raw: object) -> int | Literal["none"] | None:
    """Resolve a ``fixed_version_id`` filter value: a version id, ``"none"`` or None."""
    if is_no_value(raw):
        return NO_VALUE
    return parse_id(raw)


def normalize_assignee_param(raw: object) -> str:
    """The canonical URL spelling of an ``assigned_to_id`` value, or ``""``."""
    if is_no_value(raw):
        return NO_VALUE
    if isinstance(raw, str) and raw.strip().lower() == ME:
        return ME
    value = parse_id(raw)
    return str(value) if value is not None else ""


def normalize_version_param(raw: object) -> str:
    """The canonical URL spelling of a ``fixed_version_id`` value, or ``""``."""
    if is_no_value(raw):
        return NO_VALUE
    value = parse_id(raw)
    return str(value) if value is not None else ""


def list_query_string(params: Mapping[str, object], *, drop: tuple[str, ...] = ()) -> str:
    """Urlencode the issue list filters in *params*, without defaults and empty values.

    *drop* names parameters to leave out, e.g. the one a chip's clear link
    removes.
    """
    pairs: list[tuple[str, str]] = []
    for name in LIST_FILTER_PARAMS:
        if name in drop:
            continue
        value = params.get(name)
        if value is None:
            continue
        text = str(value).strip()
        if not text or _DEFAULTS.get(name) == text:
            continue
        pairs.append((name, text))
    return urlencode(pairs)


def saved_filter_params(definition: Mapping[str, object] | None) -> dict[str, str]:
    """The issue list query parameters a saved filter definition stands for.

    Keys the list page does not understand are ignored; ``version_id`` is
    read as ``fixed_version_id``.
    """
    out: dict[str, str] = {}
    for key, value in (definition or {}).items():
        name = _SAVED_FILTER_ALIASES.get(key, key)
        if name not in LIST_FILTER_PARAMS or value is None:
            continue
        if name == "assigned_to_id":
            text = normalize_assignee_param(value)
        elif name == "fixed_version_id":
            text = normalize_version_param(value)
        else:
            text = str(value).strip()
        if text:
            out.setdefault(name, text)
    return out
