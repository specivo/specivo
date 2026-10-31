"""SCIM PATCH (RFC 7644 section 3.5.2) applied to a resource's SCIM representation.

The service builds the resource as SCIM shows it (a dict), :func:`apply_patch`
returns a changed copy, and the service then validates and stores the result
exactly as for a PUT. So PATCH and PUT share one mapping and one set of
checks, and attributes Specivo does not keep (``title``, ``addresses``, the
enterprise extension ...) are accepted and ignored the same way.

Paths: ``attr``, ``attr.sub``, ``attr[sub eq "v"]`` and ``attr[sub eq
"v"].sub``, optionally prefixed with a schema URN; attribute names are
case-insensitive and stored under their canonical spelling. ``op`` is
case-insensitive (Entra ID sends ``Replace``). Forms identity providers send
that the RFC treats loosely are accepted on purpose:

* a ``replace``/``add`` without a path whose value object has paths as keys
  (``{"name.givenName": "A", "emails[type eq \\"work\\"].value": "a@x"}``);
* ``active`` as the string ``"False"`` (validated later, by the mapping);
* member removal as ``members[value eq "id"]`` (Okta) and as ``path:
  members`` with a list of ``{"value": id}`` (Entra ID).
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from typing import Any

from specivo.services.scim.errors import ScimError

__all__ = ["MAX_OPERATIONS", "PATCH_SCHEMA", "ScimPath", "apply_patch", "canonical_name", "parse_path"]

PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
#: More operations in one request are refused.
MAX_OPERATIONS = 1000

_OPS = frozenset({"add", "replace", "remove"})
_CORE_URNS = ("urn:ietf:params:scim:schemas:core:2.0:user:", "urn:ietf:params:scim:schemas:core:2.0:group:")

# Canonical spelling of the attributes Specivo reads or shows; others keep
# the spelling they came with.
_CANONICAL = {
    name.lower(): name
    for name in (
        "id",
        "externalId",
        "userName",
        "name",
        "formatted",
        "givenName",
        "familyName",
        "middleName",
        "displayName",
        "nickName",
        "emails",
        "value",
        "type",
        "primary",
        "display",
        "active",
        "preferredLanguage",
        "locale",
        "timezone",
        "members",
        "groups",
        "meta",
        "schemas",
    )
}

_PATH = re.compile(
    r"""
    (?P<attr>[A-Za-z][A-Za-z0-9_$-]*)
    (?:\[\s*(?P<fattr>[A-Za-z][A-Za-z0-9_-]*)\s+(?P<fop>[A-Za-z]+)\s+(?P<fvalue>"(?:[^"\\]|\\.)*")\s*\])?
    (?:\.(?P<sub>[A-Za-z][A-Za-z0-9_$-]*))?
    """,
    re.VERBOSE,
)


def canonical_name(name: str) -> str:
    """``name`` in its canonical spelling when Specivo knows it, else unchanged."""
    return _CANONICAL.get(name.lower(), name)


@dataclass(frozen=True)
class ScimPath:
    """A parsed PATCH path: ``attribute[value_filter].sub_attribute``."""

    attribute: str
    value_filter: tuple[str, str] | None = None
    sub_attribute: str | None = None


def parse_path(text: str) -> ScimPath:
    """Parse ``text``, or raise :class:`ScimError` ``invalidPath``."""
    if not isinstance(text, str) or not text.strip():
        raise ScimError.invalid_path("The path is empty.")
    path = text.strip()
    lowered = path.lower()
    for urn in _CORE_URNS:
        if lowered.startswith(urn):
            path = path[len(urn) :]
            break
    else:
        if lowered.startswith("urn:"):
            # An extension attribute (enterprise User ...): kept whole, ignored by the mapping.
            return ScimPath(path)
    match = _PATH.fullmatch(path)
    if match is None:
        raise ScimError.invalid_path("The path is not a valid SCIM attribute path.")
    value_filter: tuple[str, str] | None = None
    if match.group("fattr") is not None:
        if match.group("fop").lower() != "eq":
            raise ScimError.invalid_path("Only eq is supported in a path filter.")
        try:
            filter_value = json.loads(match.group("fvalue"))
        except ValueError:
            raise ScimError.invalid_path("The path filter value is not a valid string.") from None
        value_filter = (canonical_name(match.group("fattr")), filter_value)
    sub = match.group("sub")
    return ScimPath(
        canonical_name(match.group("attr")),
        value_filter=value_filter,
        sub_attribute=canonical_name(sub) if sub else None,
    )


# ---------------------------------------------------------------------------
# Applying operations
# ---------------------------------------------------------------------------


def _key(resource: dict[str, Any], attribute: str) -> str:
    """The key ``attribute`` is stored under in ``resource`` (case-insensitive), or ``attribute``."""
    lowered = attribute.lower()
    for key in resource:
        if key.lower() == lowered:
            return key
    return attribute


def _matches(element: Any, value_filter: tuple[str, str]) -> bool:
    if not isinstance(element, dict):
        return False
    attr, expected = value_filter
    actual = element.get(_key(element, attr))
    if isinstance(actual, str) and isinstance(expected, str):
        # ``type`` values and ids compare case-insensitively for types only.
        return actual == expected or (attr == "type" and actual.lower() == expected.lower())
    return bool(actual == expected)


def _element_key(element: Any) -> Any:
    if isinstance(element, dict) and "value" in element:
        return ("value", json.dumps(element["value"], sort_keys=True))
    return ("element", json.dumps(element, sort_keys=True, default=str))


def _merge_list(current: list[Any], new: list[Any]) -> list[Any]:
    """``current`` plus the elements of ``new`` not in it (by ``value``)."""
    seen = {_element_key(e) for e in current}
    result = list(current)
    for element in new:
        key = _element_key(element)
        if key not in seen:
            seen.add(key)
            result.append(element)
    return result


def _set(resource: dict[str, Any], op: str, path: ScimPath, value: Any) -> None:
    key = _key(resource, path.attribute)
    if path.value_filter is None and path.sub_attribute is None:
        current = resource.get(key)
        if op == "add" and isinstance(current, list):
            resource[key] = _merge_list(current, value if isinstance(value, list) else [value])
        elif op == "add" and isinstance(current, dict) and isinstance(value, dict):
            current.update({canonical_name(k): v for k, v in value.items()})
        else:
            resource[key] = value
        return

    if path.value_filter is None:
        # attr.sub on a complex attribute.
        current = resource.get(key)
        if not isinstance(current, dict):
            current = {}
            resource[key] = current
        current[_key(current, path.sub_attribute or "")] = value
        return

    elements = resource.get(key)
    if not isinstance(elements, list):
        elements = []
        resource[key] = elements
    matched = [e for e in elements if _matches(e, path.value_filter)]
    if not matched:
        if op == "replace" and path.sub_attribute is None:
            raise ScimError.no_target("No value matches the path filter.")
        filter_attr, filter_value = path.value_filter
        element: dict[str, Any] = {filter_attr: filter_value}
        if path.sub_attribute is not None:
            element[path.sub_attribute] = value
        elif isinstance(value, dict):
            element.update(value)
        elements.append(element)
        return
    for element in matched:
        if path.sub_attribute is not None:
            element[_key(element, path.sub_attribute)] = value
        elif isinstance(value, dict):
            element.clear()
            element.update(value)


def _remove(resource: dict[str, Any], path: ScimPath, value: Any) -> None:
    key = _key(resource, path.attribute)
    if key not in resource:
        return
    current = resource[key]
    if path.value_filter is None and path.sub_attribute is None:
        if isinstance(current, list) and isinstance(value, list) and value:
            # Entra ID: remove the listed members.
            drop = {_element_key(e) for e in value}
            resource[key] = [e for e in current if _element_key(e) not in drop]
        else:
            del resource[key]
        return
    if path.value_filter is None:
        if isinstance(current, dict):
            current.pop(_key(current, path.sub_attribute or ""), None)
        return
    if not isinstance(current, list):
        return
    if path.sub_attribute is None:
        resource[key] = [e for e in current if not _matches(e, path.value_filter)]
        return
    for element in current:
        if _matches(element, path.value_filter):
            element.pop(_key(element, path.sub_attribute), None)


def _operations(request: Any) -> list[dict[str, Any]]:
    if not isinstance(request, dict):
        raise ScimError.invalid_syntax("The request body must be a JSON object.")
    schemas = request.get("schemas")
    if not isinstance(schemas, list) or PATCH_SCHEMA not in schemas:
        raise ScimError.invalid_syntax(f"The request must list the {PATCH_SCHEMA} schema.")
    operations = request.get("Operations", request.get("operations"))
    if operations is None or operations == []:
        raise ScimError.invalid_value("The request has no operations.")
    if not isinstance(operations, list) or not all(isinstance(o, dict) for o in operations):
        raise ScimError.invalid_syntax("Operations must be a list of objects.")
    if len(operations) > MAX_OPERATIONS:
        raise ScimError.invalid_value(f"At most {MAX_OPERATIONS} operations are accepted in one request.")
    return operations


def apply_patch(resource: dict[str, Any], request: Any) -> dict[str, Any]:
    """A copy of ``resource`` with the PATCH ``request`` applied; :class:`ScimError` on a bad request."""
    operations = _operations(request)
    result = copy.deepcopy(resource)
    for operation in operations:
        op = operation.get("op")
        if not isinstance(op, str) or op.lower() not in _OPS:
            raise ScimError.invalid_syntax("Each operation needs an op of add, replace or remove.")
        op = op.lower()
        raw_path = operation.get("path")
        has_value = "value" in operation
        value = copy.deepcopy(operation.get("value"))

        if raw_path is None or raw_path == "":
            if op == "remove":
                raise ScimError.no_target("A remove operation needs a path.")
            if not isinstance(value, dict):
                raise ScimError.invalid_value("Without a path, the value must be an object of attributes.")
            for name, item in value.items():
                _set(result, op, parse_path(name), item)
            continue

        path = parse_path(raw_path)
        if op == "remove":
            _remove(result, path, value)
            continue
        if not has_value:
            raise ScimError.invalid_value(f"The {op} operation needs a value.")
        _set(result, op, path, value)
    return result
