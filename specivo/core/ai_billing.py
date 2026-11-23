"""AI billing accounts and payers: the values, route matching and the project payer default.

Pure functions and data, no database. The models are
:mod:`specivo.models.ai_billing_account` and the ``payer`` / ``route`` /
``billing_account_id`` columns of :mod:`specivo.models.ai_usage_entry`; the
service is :mod:`specivo.services.ai_billing_account_service`.

Two dimensions sit beside ``cost_type`` (how a cost is priced):

- **payer**: who carries the cost, ``employer`` or ``employee`` (``unknown``
  on an entry nothing decided).
- **billing account**: what the usage was paid through (a corporate gateway, a
  company plan, a personal subscription, a personal API key, a local model).
  An account with no owner is an *instance* (company) account; one with an
  owner is that person's *personal* account.

**Route** is the raw backend id a reporter saw (for example a gateway name).
It only selects a billing account through the account's ``route_patterns``
(:func:`route_matches`); it never feeds pricing, and ``provider`` stays the
model's maker.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from fnmatch import fnmatchcase
from typing import Any

#: Kinds of billing account.
BILLING_ACCOUNT_KINDS: tuple[str, ...] = ("gateway", "subscription", "api_key", "local")

#: Who pays through an account.
ACCOUNT_PAYERS: tuple[str, ...] = ("employer", "employee")
PAYER_EMPLOYER = "employer"
PAYER_EMPLOYEE = "employee"

#: Who paid for a usage entry: an account's payer, or ``unknown``.
PAYER_UNKNOWN = "unknown"
ENTRY_PAYERS: tuple[str, ...] = (*ACCOUNT_PAYERS, PAYER_UNKNOWN)

#: Cost types an account may supply when a record gives none (never ``unknown``).
ACCOUNT_DEFAULT_COST_TYPES: tuple[str, ...] = ("subscription", "api", "local", "free")

#: Slug of a billing account, the value a reporter sends as ``billing_account``:
#: lower case letters, digits, ``.``, ``_`` and ``-``, 1 to 64 characters.
BILLING_ACCOUNT_SLUG_MAX_LENGTH = 64
BILLING_ACCOUNT_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")

#: Longest route an entry stores and longest route pattern.
ROUTE_MAX_LENGTH = 100
MAX_ROUTE_PATTERNS = 20

#: Key of ``projects.settings`` naming who pays for usage on the project when
#: no billing account decides: ``employer``, ``employee`` or ``none``.
PROJECT_PAYER_DEFAULT_KEY = "ai_usage_payer_default"
PAYER_DEFAULT_NONE = "none"
PROJECT_PAYER_DEFAULTS: tuple[str, ...] = (*ACCOUNT_PAYERS, PAYER_DEFAULT_NONE)


def normalize_account_slug(value: object) -> str | None:
    """A billing account slug in lower case, or ``None`` when *value* is not a valid slug."""
    if not isinstance(value, str):
        return None
    slug = value.strip().lower()
    return slug if BILLING_ACCOUNT_SLUG_RE.match(slug) else None


def normalize_route(value: object) -> str | None:
    """A route as stored on an entry: trimmed, at most :data:`ROUTE_MAX_LENGTH` characters; blank is ``None``.

    The case is kept (it is the raw backend id); matching ignores it.
    """
    if not isinstance(value, str):
        return None
    route = value.strip()
    return route[:ROUTE_MAX_LENGTH] if route else None


def normalize_route_pattern(value: object) -> str | None:
    """A route pattern in lower case, or ``None`` when it is blank, too long or holds whitespace."""
    if not isinstance(value, str):
        return None
    pattern = value.strip().lower()
    if not pattern or len(pattern) > ROUTE_MAX_LENGTH or any(ch.isspace() for ch in pattern):
        return None
    return pattern


def route_matches(patterns: Iterable[str], route: str | None) -> bool:
    """Whether *route* matches one of *patterns* (``fnmatch``, case-insensitive).

    No route matches nothing, and no patterns match nothing.
    """
    if not route:
        return False
    subject = route.strip().lower()
    return any(fnmatchcase(subject, pattern.lower()) for pattern in patterns)


def covers_tool(tool_slugs: Iterable[str], tool_slug: str | None) -> bool:
    """Whether an account listing *tool_slugs* covers *tool_slug*: an empty list covers every tool."""
    slugs = list(tool_slugs)
    return not slugs or (tool_slug is not None and tool_slug in slugs)


def project_payer_default(project_settings: Mapping[str, Any] | None) -> str:
    """The project's payer default: ``employer``, ``employee`` or ``none`` (missing or unusable values)."""
    value = (project_settings or {}).get(PROJECT_PAYER_DEFAULT_KEY)
    return value if value in PROJECT_PAYER_DEFAULTS else PAYER_DEFAULT_NONE


def payer_from_default(default: str) -> str:
    """The entry payer a project default gives: its payer, or ``unknown`` for ``none``."""
    return default if default in ACCOUNT_PAYERS else PAYER_UNKNOWN
