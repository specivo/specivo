"""The import bundle's JSON Schemas agree with the IR they are derived from.

The format is the IR written to disk, so each record schema must describe its
dataclass field by field: the same names (allowing for the renames the format
declares), the same required fields, JSON types that carry each field's Python
type, and enums with the same values. Checked here rather than trusted, so a
field added to the IR without a schema change, or the reverse, fails a test
instead of producing bundles core silently half-reads.
"""

from __future__ import annotations

import dataclasses
import json
import types
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Union, get_args, get_origin, get_type_hints
from urllib.parse import urljoin

import pytest
from jsonschema import Draft202012Validator

from specivo.importers.bundle.codec import json_name
from specivo.importers.bundle.format import EXTRA, RECORD_FILES, SCHEMA_DIR, SCHEMA_ID_BASE
from specivo.importers.bundle.scan import schema_registry, schemas
from specivo.importers.core.ir import (
    IRActivity,
    IRPriority,
    IRPublicAccess,
    IRRole,
    IRStatus,
    IRTracker,
)

pytestmark = pytest.mark.unit

PUBLISHED = Path(__file__).resolve().parents[3] / "docs" / "import-bundle" / "v1"

LOOKUPS = {
    "tracker": IRTracker,
    "status": IRStatus,
    "priority": IRPriority,
    "activity": IRActivity,
    "role": IRRole,
}


def _resolve_in(base: str, schema: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Return the concrete alternatives *schema* allows, following $ref and anyOf.

    *base* is the schema file *schema* was found in, which relative refs are
    resolved against. Each alternative comes with the file it was found in.
    """
    if "$ref" in schema:
        uri = urljoin(SCHEMA_ID_BASE + base, schema["$ref"])
        resolved = schema_registry().resolver().lookup(uri).contents
        return _resolve_in(uri.split("#")[0].rsplit("/", 1)[-1], resolved)
    if "anyOf" in schema:
        return [alternative for branch in schema["anyOf"] for alternative in _resolve_in(base, branch)]
    return [(base, schema)]


def _types(alternatives: list[tuple[str, dict[str, Any]]]) -> set[str]:
    found: set[str] = set()
    for _, alternative in alternatives:
        declared = alternative.get("type")
        if isinstance(declared, list):
            found.update(declared)
        elif declared:
            found.add(declared)
    return found


def _optional(hint: Any) -> tuple[Any, bool]:
    """Return ``(inner hint, whether None is allowed)``."""
    if get_origin(hint) in (Union, types.UnionType):
        inner = [arg for arg in get_args(hint) if arg is not type(None)]
        assert len(inner) == 1, hint
        return inner[0], True
    return hint, False


COMMON = schemas()["common.schema.json"]["$defs"]


def _check_value(base: str, where: str, hint: Any, prop: dict[str, Any]) -> None:
    """Assert the property schema *prop* carries values of Python type *hint*."""
    inner, nullable = _optional(hint)
    alternatives = _resolve_in(base, prop)
    found = _types(alternatives)

    if inner is Any:
        assert not found, f"{where}: an Any field must accept every JSON value, schema says {found}"
        return

    assert ("null" in found) == nullable, f"{where}: null allowed in schema {'null' in found}, IR {nullable}"
    found.discard("null")
    concrete = [schema for _, schema in alternatives if _types([(base, schema)]) - {"null"}]
    concrete_base = next((file for file, schema in alternatives if _types([(file, schema)]) - {"null"}), base)

    if inner is bool:
        assert found == {"boolean"}, where
    elif inner is int:
        assert found == {"integer"}, where
    elif inner is str:
        assert found == {"string"}, where
    elif inner is Decimal:
        assert found == {"number", "string"}, where
    elif inner is datetime:
        assert found == {"string"} and concrete[0].get("pattern") == COMMON["dateTime"]["pattern"], where
    elif inner is date:
        assert found == {"string"} and concrete[0].get("pattern") == COMMON["date"]["pattern"], where
    elif isinstance(inner, type) and issubclass(inner, Enum):
        assert found == {"string"}, where
        assert sorted(concrete[0]["enum"]) == sorted(member.value for member in inner), f"{where}: enum differs"
    elif get_origin(inner) is list:
        assert found == {"array"}, where
        (item_hint,) = get_args(inner)
        _check_value(concrete_base, f"{where}[]", item_hint, concrete[0]["items"])
    elif get_origin(inner) is dict:
        assert found == {"object"}, where
    elif dataclasses.is_dataclass(inner):
        assert found == {"object"}, where
        _check_record(concrete_base, where, inner, concrete[0])
    else:  # pragma: no cover - a new IR field type needs a case here
        pytest.fail(f"{where}: no rule for IR type {inner!r}")


def _check_record(base: str, where: str, cls: type, schema: dict[str, Any]) -> None:
    """Assert *schema* describes dataclass *cls* field by field."""
    hints = get_type_hints(cls)
    fields = {json_name(cls, field.name): field for field in dataclasses.fields(cls)}
    properties = dict(schema["properties"])
    properties.pop(EXTRA, None)

    assert set(properties) == set(fields), (
        f"{where}: schema has {sorted(set(properties) - set(fields))} the IR lacks, "
        f"IR has {sorted(set(fields) - set(properties))} the schema lacks"
    )
    required = {
        name
        for name, field in fields.items()
        if field.default is dataclasses.MISSING and field.default_factory is dataclasses.MISSING
    }
    assert set(schema.get("required", [])) == required, f"{where}: required fields differ"
    assert schema.get("additionalProperties") is False, f"{where}: records must be closed"

    for name, field in fields.items():
        _check_value(base, f"{where}.{name}", hints[field.name], properties[name])


class TestSchemasAgreeWithTheIR:
    @pytest.mark.parametrize("record_file", RECORD_FILES, ids=lambda f: f.name)
    def test_each_record_file(self, record_file):
        schema = schemas()[record_file.schema]
        _check_record(record_file.schema, record_file.name, record_file.ir_type, schema)

    def test_every_record_may_carry_extra(self):
        for record_file in RECORD_FILES:
            assert schemas()[record_file.schema]["properties"][EXTRA] == {"$ref": "common.schema.json#/$defs/extra"}

    @pytest.mark.parametrize("name", sorted(LOOKUPS))
    def test_each_lookup(self, name):
        lookups = schemas()["lookups.schema.json"]
        _check_record("lookups.schema.json", name, LOOKUPS[name], lookups["$defs"][name])

    def test_lookups_lists_are_the_irs(self):
        from specivo.importers.bundle.format import LOOKUP_LISTS
        from specivo.importers.core.ir import IRLookups

        lookups = schemas()["lookups.schema.json"]
        assert set(lookups["properties"]) == set(LOOKUP_LISTS) == {f.name for f in dataclasses.fields(IRLookups)}

    def test_public_access(self):
        schema = schemas()["public_access.schema.json"]
        _check_record("public_access.schema.json", "public_access", IRPublicAccess, schema)

    def test_the_attachment_hash_stands_in_for_the_storage_key(self):
        properties = schemas()["attachments.schema.json"]["properties"]
        assert "sha256" in properties and "storage_key" not in properties


class TestSchemaFiles:
    def test_every_schema_is_valid_draft_2020_12(self):
        for name, schema in schemas().items():
            Draft202012Validator.check_schema(schema)
            assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema", name

    def test_every_schema_has_its_own_id(self):
        for name, schema in schemas().items():
            assert schema["$id"] == SCHEMA_ID_BASE + name

    def test_one_schema_per_file_type_plus_the_manifest(self):
        expected = {f.schema for f in RECORD_FILES} | {
            "manifest.schema.json",
            "lookups.schema.json",
            "public_access.schema.json",
            "common.schema.json",
        }
        assert set(schemas()) == expected

    def test_the_published_copy_is_identical(self):
        """docs/import-bundle/v1 is what importer authors read; core validates
        against the package copy. They must not drift apart."""
        packaged = {path.name: path.read_bytes() for path in SCHEMA_DIR.glob("*.schema.json")}
        published = {path.name: path.read_bytes() for path in PUBLISHED.glob("*.schema.json")}
        assert published == packaged

    def test_the_spec_names_every_schema(self):
        spec = (PUBLISHED.parent / "v1.md").read_text()
        for name in schemas():
            if name != "common.schema.json":
                assert name in spec, name

    def test_schemas_are_formatted_json(self):
        """Stable formatting keeps the published copy reviewable."""
        for path in SCHEMA_DIR.glob("*.schema.json"):
            text = path.read_text()
            assert text == json.dumps(json.loads(text), indent=2, ensure_ascii=False) + "\n", path.name
