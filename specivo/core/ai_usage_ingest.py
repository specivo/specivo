"""The AI usage ingest contract v1: versions, limits, schema checks and record parsing.

Pure functions, no database. The contract is published in
``docs/contracts/ai-usage/v1/`` (README and JSON Schemas); the request schemas
the server validates against are a copy in ``specivo/data/contracts/ai-usage/v1/``,
and a unit test keeps the two identical.

A record is checked in two passes:

1. the JSON Schema (``record.schema.json``). Properties the schema does not
   define are **ignored with a warning** (``unknown_fields``) rather than
   refused, so a client written for a later minor revision of v1 still works
   against this server; every other schema error refuses the record.
2. the rules a schema cannot state (:func:`parse_record`): real calendar
   dates, the time window of ``usage_date``, ``ended_at`` after
   ``started_at``, and the age of ``usage_date``.

The database checks (currency codes, the target user, permissions,
attribution) are :class:`specivo.services.ai_usage_ingest_service.AiUsageIngestService`'s.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as SchemaError
from referencing import Registry, Resource

from specivo.core.ai_usage import utc_midnight

SCHEMA_V1 = "specivo.ai-usage.v1"
#: Versions accepted in a batch's ``schema``, newest first. The server keeps
#: the current and the previous major version.
SUPPORTED_SCHEMAS: tuple[str, ...] = (SCHEMA_V1,)

MAX_RECORDS = 500
MAX_BYTES = 1024 * 1024
#: Oldest ``usage_date`` accepted, in days before today (UTC).
MAX_AGE_DAYS = 400
#: How far ahead of the server's clock an instant or a day may be.
CLOCK_SKEW = timedelta(minutes=10)
#: Deepest nesting of objects and arrays a body may have (a valid one has 4).
MAX_JSON_DEPTH = 16

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "data" / "contracts" / "ai-usage" / "v1"
SCHEMA_ID_BASE = "https://specivo.io/schemas/ai-usage/v1/"

#: ``attribution.issue_ref_source`` → ``attribution_method`` of an explicit issue.
ISSUE_REF_METHODS: dict[str, str] = {"explicit": "explicit", "env": "env"}


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


@cache
def schemas() -> dict[str, dict[str, Any]]:
    """The request schemas by file name."""
    return {path.name: json.loads(path.read_text()) for path in sorted(SCHEMA_DIR.glob("*.schema.json"))}


@cache
def schema_registry() -> Registry[Any]:
    """A registry holding every schema under its ``$id``, for ``$ref`` resolution."""
    resources = [(schema["$id"], Resource.from_contents(schema)) for schema in schemas().values()]
    return Registry().with_resources(resources)


@cache
def record_validator() -> Draft202012Validator:
    return Draft202012Validator(schemas()["record.schema.json"], registry=schema_registry())


@cache
def batch_validator() -> Draft202012Validator:
    return Draft202012Validator(schemas()["batch.schema.json"], registry=schema_registry())


# ---------------------------------------------------------------------------
# Problems
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Problem:
    """A warning or an error about one record (or the batch): a code, a message and a field path."""

    code: str
    message: str
    field: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "field": self.field}


class RecordRejectedError(Exception):
    """A record the server refuses; carries the :class:`Problem`."""

    def __init__(self, code: str, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.problem = Problem(code, message, field)


def _path(parts: Any) -> str | None:
    text = ".".join(str(part) for part in parts)
    return text or None


def _extra_properties(error: SchemaError) -> list[str]:
    instance = error.instance if isinstance(error.instance, Mapping) else {}
    declared = error.schema.get("properties", {}) if isinstance(error.schema, Mapping) else {}
    return sorted(str(name) for name in instance if name not in declared)


def _message(error: SchemaError) -> str:
    """A schema error as a short sentence; the offending value is never echoed for long strings."""
    where = _path(error.absolute_path) or "the record"
    validator = error.validator
    if validator == "required":
        return error.message.replace("is a required property", "is required")
    if validator == "pattern" or validator == "format":
        return f"{where} does not have the expected format"
    if validator in ("maxLength", "minLength"):
        bound = "at most" if validator == "maxLength" else "at least"
        return f"{where} must be {bound} {error.validator_value} characters long"
    if validator in ("minimum", "maximum"):
        return f"{where} must be {'at least' if validator == 'minimum' else 'at most'} {error.validator_value}"
    if validator in ("maxItems", "minItems"):
        return f"{where} must have {'at most' if validator == 'maxItems' else 'at least'} {error.validator_value} items"
    if validator == "type":
        return f"{where} must be of type {error.validator_value}"
    if validator in ("enum", "const"):
        return f"{where} must be one of the values the contract defines"
    return f"{where} is not valid"


def _matching_branch(error: SchemaError) -> list[SchemaError] | None:
    """The errors of the one ``anyOf`` branch whose type the value has (the object, not the null).

    ``None`` when no branch, or more than one, matches the value's type.
    """
    branches: dict[Any, list[SchemaError]] = {}
    for sub in error.context or []:
        branches.setdefault(sub.relative_schema_path[0], []).append(sub)
    typed = [
        errors
        for errors in branches.values()
        if not any(e.validator in ("type", "const", "enum") and not e.relative_path for e in errors)
    ]
    return typed[0] if len(typed) == 1 else None


def _sorted(errors: Any) -> list[SchemaError]:
    return sorted(errors, key=lambda e: [str(p) for p in e.absolute_path])


def check_schema(
    validator: Draft202012Validator, instance: Any, *, skip: Any = None
) -> tuple[list[str], Problem | None]:
    """Validate *instance*: the unknown properties found (dotted paths, sorted), and the first other error.

    Unknown properties are collected inside nullable objects too (``anyOf``
    with ``null``). *skip* is a predicate over an error's absolute path;
    errors it accepts are ignored (the batch check leaves the records to the
    per-record check).
    """
    unknown: list[str] = []
    first: SchemaError | None = None
    pending = _sorted(validator.iter_errors(instance))
    while pending:
        error = pending.pop(0)
        if skip is not None and skip(list(error.absolute_path)):
            continue
        if error.validator == "additionalProperties":
            prefix = _path(error.absolute_path)
            unknown.extend(f"{prefix}.{name}" if prefix else name for name in _extra_properties(error))
            continue
        if error.validator in ("anyOf", "oneOf"):
            branch = _matching_branch(error)
            if branch is not None:
                pending = _sorted([*branch, *pending])
                continue
        if first is None:
            first = error
    problem = None
    if first is not None:
        problem = Problem("invalid_record", _message(first), _path(first.absolute_path))
    return sorted(unknown), problem


def strip_unknown(value: Any, unknown: list[str]) -> Any:
    """A copy of *value* without the properties at the dotted paths *unknown*."""
    if not unknown:
        return value
    result = json.loads(json.dumps(value))
    for dotted in unknown:
        *parents, name = dotted.split(".")
        node: Any = result
        for part in parents:
            node = node[int(part)] if isinstance(node, list) else node.get(part)
            if node is None:
                break
        if isinstance(node, dict):
            node.pop(name, None)
    return result


def unknown_fields_warning(unknown: list[str]) -> Problem:
    names = ", ".join(unknown[:10]) + (" and more" if len(unknown) > 10 else "")
    return Problem("unknown_fields", f"Ignored properties this server does not know: {names}", unknown[0])


def too_deep(value: Any, limit: int = MAX_JSON_DEPTH) -> bool:
    """Whether *value* nests objects and arrays deeper than *limit* (checked without recursion)."""
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, dict):
            children: Any = item.values()
        elif isinstance(item, list):
            children = item
        else:
            continue
        if depth > limit:
            return True
        stack.extend((child, depth + 1) for child in children)
    return False


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass
class Attribution:
    """The attribution hints of a record (all optional)."""

    issue_ref: str | None = None
    issue_method: str = "explicit"
    project_key: str | None = None
    git_branch: str | None = None
    repo: str | None = None
    commits: list[str] = field(default_factory=list)

    @property
    def is_explicit(self) -> bool:
        return self.issue_ref is not None or self.project_key is not None


@dataclass
class IngestRecord:
    """One record after both passes, with typed values."""

    idempotency_key: str
    tool: str
    model: str
    usage_date: date
    tokens: dict[str, int]
    provider: str | None = None
    session_id: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    request_count: int = 0
    long_context: dict[str, int] | None = None
    reported_amount: str | None = None
    reported_currency: str | None = None
    cost_type: str | None = None
    on_behalf_of: str | None = None
    attribution: Attribution = field(default_factory=Attribution)


def _instant(value: str | None, name: str) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise RecordRejectedError("invalid_record", f"{name} is not a valid timestamp", name) from None
    return parsed.astimezone(UTC)


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def parse_record(raw: Mapping[str, Any], now: datetime) -> IngestRecord:
    """Turn a schema-valid record into an :class:`IngestRecord`, checking the rules a schema cannot state.

    Raises :class:`RecordRejectedError` (``invalid_record``) on the first broken rule.
    """
    try:
        usage_date = date.fromisoformat(raw["usage_date"])
    except ValueError:
        raise RecordRejectedError("invalid_record", "usage_date is not a valid date", "usage_date") from None
    today = now.astimezone(UTC).date()
    if usage_date > (now + CLOCK_SKEW).astimezone(UTC).date():
        raise RecordRejectedError("invalid_record", "usage_date must not be in the future (UTC)", "usage_date")
    if usage_date < today - timedelta(days=MAX_AGE_DAYS):
        raise RecordRejectedError(
            "invalid_record", f"usage_date must not be more than {MAX_AGE_DAYS} days ago", "usage_date"
        )

    day_start = utc_midnight(usage_date)
    day_end = day_start + timedelta(days=1)
    started = _instant(raw.get("started_at"), "started_at")
    ended = _instant(raw.get("ended_at"), "ended_at")
    latest = now + CLOCK_SKEW
    if started is not None:
        if not day_start <= started < day_end:
            raise RecordRejectedError("invalid_record", "started_at must fall on usage_date (a UTC day)", "started_at")
        if started > latest:
            raise RecordRejectedError("invalid_record", "started_at must not be in the future", "started_at")
    if ended is not None:
        if not day_start <= ended <= day_end:
            raise RecordRejectedError(
                "invalid_record", "ended_at must fall on usage_date (a UTC day) or at its end", "ended_at"
            )
        if ended > latest:
            raise RecordRejectedError("invalid_record", "ended_at must not be in the future", "ended_at")
        if started is not None and ended < started:
            raise RecordRejectedError("invalid_record", "ended_at must not be before started_at", "ended_at")

    tokens = {str(k): int(v) for k, v in (raw.get("tokens") or {}).items()}
    long_context_raw = raw.get("long_context_tokens")
    long_context = {str(k): int(v) for k, v in long_context_raw.items()} if long_context_raw else None

    reported = raw.get("reported_cost")
    attribution_raw = raw.get("attribution") or {}
    issue_ref = _clean(attribution_raw.get("issue_ref"))
    project_key = _clean(attribution_raw.get("project_key"))
    attribution = Attribution(
        issue_ref=issue_ref.upper() if issue_ref else None,
        issue_method=ISSUE_REF_METHODS[attribution_raw.get("issue_ref_source") or "explicit"],
        project_key=project_key.upper() if project_key else None,
        git_branch=_clean(attribution_raw.get("git_branch")),
        repo=_clean(attribution_raw.get("repo")),
        commits=list(dict.fromkeys(sha.lower() for sha in attribution_raw.get("commits") or [])),
    )
    return IngestRecord(
        idempotency_key=raw["idempotency_key"],
        tool=raw["tool"],
        model=raw["model"],
        usage_date=usage_date,
        tokens=tokens,
        provider=_clean(raw.get("provider")),
        session_id=_clean(raw.get("session_id")),
        started_at=started,
        ended_at=ended,
        request_count=int(raw.get("request_count") or 0),
        long_context=long_context,
        reported_amount=reported["amount"] if reported else None,
        reported_currency=reported["currency"] if reported else None,
        cost_type=raw.get("cost_type"),
        on_behalf_of=_clean(raw.get("on_behalf_of")),
        attribution=attribution,
    )


# ---------------------------------------------------------------------------
# Repository hints
# ---------------------------------------------------------------------------

_SCP_LIKE = re.compile(r"^[\w.\-]+@(?P<host>[\w.\-]+):(?P<path>.+)$")


def repo_full_name(hint: str | None) -> str | None:
    """The ``owner/name`` path of a repository hint, lower case; ``None`` when there is none.

    Accepts ``github.com/acme/app``, ``https://github.com/acme/app.git``,
    ``git@github.com:acme/app.git``, ``ssh://git@host:22/group/sub/app`` and
    a bare ``acme/app``. The host is dropped: integrations are matched by
    their ``repo_full_name``, which carries none.
    """
    if not hint:
        return None
    text = hint.strip()
    scp = _SCP_LIKE.match(text)
    if scp is not None and "://" not in text:
        path = scp.group("path")
    else:
        if "://" in text:
            text = text.split("://", 1)[1]
            text = text.split("@", 1)[-1] if "@" in text.split("/", 1)[0] else text
            path = text.split("/", 1)[1] if "/" in text else ""
        else:
            first, _, rest = text.partition("/")
            path = rest if "." in first or ":" in first else text
    path = path.split("?", 1)[0].split("#", 1)[0].strip("/")
    path = path.removesuffix(".git").strip("/")
    if path.count("/") < 1 or any(not part for part in path.split("/")):
        return None
    return path.lower()
