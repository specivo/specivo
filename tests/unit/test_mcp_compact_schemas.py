"""The advertised tool schemas are lean, and only the advertised copy changes.

``compact_input_schema`` drops ``title`` keywords, the ``null`` branch of optional
fields and ``default: null``; tools return text without an output schema. Arguments
are still validated against the original ``Tool.parameters``.
"""

from __future__ import annotations

import copy
import json
from typing import Annotated, Any

import pytest
from mcp import Client
from pydantic import BaseModel, Field

from specivo.mcp.compact_server import SpecivoMCPServer, compact_input_schema
from specivo.mcp.server import mcp

pytestmark = pytest.mark.unit


def test_titles_are_dropped_but_a_property_named_title_stays() -> None:
    schema = {
        "title": "create_wikiArguments",
        "type": "object",
        "properties": {
            "title": {"title": "Title", "type": "string", "description": "Page title."},
            "text": {"title": "Text", "type": "string"},
        },
        "required": ["title", "text"],
    }
    assert compact_input_schema(schema) == {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Page title."},
            "text": {"type": "string"},
        },
        "required": ["title", "text"],
    }


def test_an_optional_field_collapses_to_its_type() -> None:
    field = {
        "anyOf": [{"type": "integer", "minimum": 1}, {"type": "null"}],
        "default": None,
        "description": "Version ID.",
        "title": "Fixed Version Id",
    }
    assert compact_input_schema({"properties": {"v": field}}) == {
        "properties": {"v": {"type": "integer", "minimum": 1, "description": "Version ID."}}
    }


def test_non_null_defaults_enums_and_required_are_kept() -> None:
    schema = {
        "properties": {
            "op": {"enum": ["get", "set"], "type": "string", "title": "Op"},
            "limit": {"type": "integer", "default": 25, "maximum": 100, "title": "Limit"},
            "flag": {"type": "boolean", "default": False},
            "mode": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": "hybrid"},
        },
        "required": ["op"],
    }
    assert compact_input_schema(schema) == {
        "properties": {
            "op": {"enum": ["get", "set"], "type": "string"},
            "limit": {"type": "integer", "default": 25, "maximum": 100},
            "flag": {"type": "boolean", "default": False},
            "mode": {"type": "string", "default": "hybrid"},
        },
        "required": ["op"],
    }


def test_a_wider_union_only_loses_its_null_branch() -> None:
    field = {"anyOf": [{"type": "string"}, {"type": "integer"}, {"type": "null"}], "default": None}
    assert compact_input_schema(field) == {"anyOf": [{"type": "string"}, {"type": "integer"}]}


def test_defs_items_and_refs_are_compacted_recursively() -> None:
    schema = {
        "$defs": {
            "Period": {
                "title": "Period",
                "type": "object",
                "properties": {
                    "ends_on": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None, "title": "Ends On"},
                },
            }
        },
        "properties": {
            "periods": {
                "anyOf": [{"type": "array", "items": {"$ref": "#/$defs/Period"}}, {"type": "null"}],
                "default": None,
                "title": "Periods",
            },
            "tags": {"type": "array", "items": {"anyOf": [{"type": "string"}, {"type": "null"}], "title": "Tag"}},
        },
    }
    assert compact_input_schema(schema) == {
        "$defs": {"Period": {"type": "object", "properties": {"ends_on": {"type": "string"}}}},
        "properties": {
            "periods": {"type": "array", "items": {"$ref": "#/$defs/Period"}},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
    }


def test_literal_values_are_not_walked_into() -> None:
    default = {"title": "kept", "nested": None}
    schema = {"type": "object", "default": default, "enum": [{"title": "x"}], "const": {"title": "y"}}
    assert compact_input_schema(schema) == schema


def test_a_branch_clashing_with_its_parent_keeps_the_union() -> None:
    field = {"anyOf": [{"type": "string", "description": "inner"}, {"type": "null"}], "description": "outer"}
    expected = {"anyOf": [{"type": "string", "description": "inner"}], "description": "outer"}
    assert compact_input_schema(field) == expected


def test_the_input_is_not_modified() -> None:
    schema = {"title": "A", "properties": {"x": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None}}}
    before = copy.deepcopy(schema)
    compact_input_schema(schema)
    assert schema == before


def _walk(node: Any) -> list[dict[str, Any]]:
    """Every schema object below *node* (property maps are walked by value)."""
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        found.append(node)
        for key, value in node.items():
            if key in ("properties", "$defs"):
                for sub in value.values():
                    found.extend(_walk(sub))
            elif key in ("default", "enum", "const", "examples"):
                continue
            else:
                found.extend(_walk(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk(item))
    return found


async def test_the_registered_tools_advertise_lean_schemas_and_no_output_schema() -> None:
    tools = await mcp.list_tools()
    assert len(tools) > 50
    for tool in tools:
        assert tool.output_schema is None, tool.name
        for node in _walk(tool.input_schema):
            assert "title" not in node, (tool.name, node)
            assert node.get("default", 0) is not None, (tool.name, node)
            for union in ("anyOf", "oneOf"):
                assert {"type": "null"} not in node.get(union, []), (tool.name, node)


async def test_validation_keeps_the_original_schema() -> None:
    """The registered parameters still say the field is nullable; only the listing is lean."""
    registered = {tool.name: tool for tool in mcp._tool_manager.list_tools()}
    original = json.dumps(registered["specivo_update_issue"].parameters)
    assert '"type": "null"' in original and '"title"' in original


class _Item(BaseModel):
    name: str
    note: str | None = None


async def test_an_explicit_null_is_still_accepted_and_the_result_is_text_only() -> None:
    server = SpecivoMCPServer("compact-test")

    @server.tool()
    async def echo(
        limit: Annotated[int | None, Field(description="Max.")] = None,
        items: list[_Item] | None = None,
    ) -> str:
        return f"limit={limit} items={len(items or [])}"

    [listed] = await server.list_tools()
    assert listed.output_schema is None
    assert listed.input_schema["properties"]["limit"] == {"type": "integer", "description": "Max."}

    async with Client(server) as client:
        result = await client.call_tool("echo", {"limit": None, "items": [{"name": "a", "note": None}]})
    assert result.is_error is False
    assert result.structured_content is None
    assert [block.text for block in result.content] == ["limit=None items=1"]  # type: ignore[union-attr]
