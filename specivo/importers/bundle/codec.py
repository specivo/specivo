"""Convert IR dataclasses to and from bundle records.

One generic converter, driven by each dataclass's type hints, rather than a
hand-written function per record type: the bundle format is derived from the IR
field by field, so a field added to the IR needs a schema change and nothing
here.

Records are assumed to have passed schema validation already. Decoding a
record that did not raises whatever the bad value provokes.
"""

from __future__ import annotations

import dataclasses
import types
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Union, get_args, get_origin, get_type_hints

from specivo.importers.bundle.format import RENAMED_FIELDS
from specivo.importers.core.ir import IRActivity, IRPriority, IRRole, IRStatus, IRTracker

# The IR class of each list in lookups.json.
LOOKUP_TYPES: dict[str, type] = {
    "trackers": IRTracker,
    "statuses": IRStatus,
    "priorities": IRPriority,
    "activities": IRActivity,
    "roles": IRRole,
}

_HINTS: dict[type, dict[str, Any]] = {}


def _hints(cls: type) -> dict[str, Any]:
    """Return *cls*'s resolved type hints, worked out once per class."""
    hints = _HINTS.get(cls)
    if hints is None:
        hints = _HINTS[cls] = get_type_hints(cls)
    return hints


def json_name(cls: type, field_name: str) -> str:
    """Return the bundle property an IR field is written as."""
    return RENAMED_FIELDS.get((cls, field_name), field_name)


def to_record(obj: Any) -> dict[str, Any]:
    """Return the bundle record for IR dataclass *obj*.

    Fields that are ``None`` are left out, which the format treats the same as
    an explicit ``null``.
    """
    cls = type(obj)
    record: dict[str, Any] = {}
    for field in dataclasses.fields(obj):
        value = getattr(obj, field.name)
        if value is None:
            continue
        record[json_name(cls, field.name)] = _encode(value)
    return record


def _encode(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError(f"Timestamp {value.isoformat()} has no UTC offset; a bundle needs one")
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        # A string keeps the exact value; a JSON number would go through float.
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return to_record(value)
    if isinstance(value, list | tuple):
        return [_encode(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _encode(item) for key, item in value.items()}
    return value


def from_record[T](cls: type[T], record: dict[str, Any]) -> T:
    """Build IR dataclass *cls* from a bundle record.

    Properties the IR does not have (``extra``) are ignored, and an absent
    property takes the field's default.
    """
    hints = _hints(cls)
    kwargs: dict[str, Any] = {}
    for field in dataclasses.fields(cls):  # type: ignore[arg-type]
        name = json_name(cls, field.name)
        if name not in record:
            continue
        kwargs[field.name] = _decode(hints[field.name], record[name])
    return cls(**kwargs)


def _decode(hint: Any, value: Any) -> Any:
    origin = get_origin(hint)

    if origin in (Union, types.UnionType):
        if value is None:
            return None
        (inner,) = [arg for arg in get_args(hint) if arg is not type(None)]
        return _decode(inner, value)
    if value is None:
        # Only reachable for a record that skipped schema validation.
        return None
    if origin is list:
        (item_hint,) = get_args(hint)
        return [_decode(item_hint, item) for item in value]
    if origin is dict:
        _, value_hint = get_args(hint)
        return {key: _decode(value_hint, item) for key, item in value.items()}
    if hint is Any:
        return value
    if hint is datetime:
        return datetime.fromisoformat(value)
    if hint is date:
        return date.fromisoformat(value)
    if hint is Decimal:
        # str() of a float is its shortest round-tripping form, so 1.1 stays 1.1.
        return Decimal(str(value))
    if isinstance(hint, type) and issubclass(hint, Enum):
        return hint(value)
    if isinstance(hint, type) and dataclasses.is_dataclass(hint):
        return from_record(hint, value)
    return value


def value_problems(cls: type, record: dict[str, Any], prefix: str = "") -> list[str]:
    """Return what in *record* would not decode into *cls*, one message per value.

    The schemas check the shape of a date or a timestamp but not that it
    exists (``2024-02-30``); this parses every one, so a bad value is reported
    with its place in the record instead of failing the import half way.
    """
    hints = _hints(cls)
    problems: list[str] = []
    for field in dataclasses.fields(cls):
        name = json_name(cls, field.name)
        if record.get(name) is not None:
            problems.extend(_value_problems(hints[field.name], record[name], f"{prefix}/{name}"))
    return problems


def _value_problems(hint: Any, value: Any, pointer: str) -> list[str]:
    origin = get_origin(hint)
    if origin in (Union, types.UnionType):
        if value is None:
            return []
        (inner,) = [arg for arg in get_args(hint) if arg is not type(None)]
        return _value_problems(inner, value, pointer)
    if origin is list and isinstance(value, list):
        (item_hint,) = get_args(hint)
        return [p for index, item in enumerate(value) for p in _value_problems(item_hint, item, f"{pointer}/{index}")]
    if isinstance(hint, type) and dataclasses.is_dataclass(hint) and isinstance(value, dict):
        return value_problems(hint, value, pointer)
    try:
        if hint is datetime:
            datetime.fromisoformat(value)
        elif hint is date:
            date.fromisoformat(value)
        elif hint is Decimal and not Decimal(str(value)).is_finite():
            return [f"{pointer}: {value!r} is not a finite number"]
    except (ValueError, TypeError):
        kind = "timestamp" if hint is datetime else "date" if hint is date else "number"
        return [f"{pointer}: {value!r} is not a valid {kind}"]
    return []
