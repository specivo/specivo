"""The Specivo MCP server class: a lean ``tools/list``.

Every client without a tool search of its own puts the whole ``tools/list``
answer into the model's prompt on every turn, so what it advertises is kept
short without changing what a tool accepts:

- **No output schemas.** Every tool returns text. Left to itself the SDK
  derives a ``{"result": string}`` output schema from the ``str`` return
  annotation and repeats the text as ``structuredContent``; tools are
  registered with ``structured_output=False`` by default instead, so a result
  is the text content alone.
- **Lean input schemas.** ``compact_input_schema`` drops what Pydantic adds
  for humans (``title`` on every field and on the arguments object) and the
  ``null`` branch of optional fields (``anyOf: [X, {"type": "null"}]`` with
  ``default: null`` becomes ``X``, still left out of ``required``).

Only the advertised list changes. Arguments are validated against the tool's
original ``parameters`` (``Tool.parameters`` and the argument model built from
the function signature), so an explicit ``null`` is still accepted where the
function takes ``None``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import Icon, ToolAnnotations
from mcp.types import Tool as MCPTool

#: Keywords whose value is a map of names to subschemas: the keys are names, not keywords.
_SCHEMA_MAPS = frozenset({"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"})
#: Keywords whose value is one subschema.
_SCHEMA_VALUES = frozenset(
    {
        "items",
        "additionalProperties",
        "unevaluatedProperties",
        "unevaluatedItems",
        "contains",
        "propertyNames",
        "not",
        "if",
        "then",
        "else",
    }
)
#: Keywords whose value is a list of subschemas.
_SCHEMA_LISTS = frozenset({"anyOf", "oneOf", "allOf", "prefixItems"})
#: Annotation keywords that only help a human reader.
_DROPPED_KEYWORDS = frozenset({"title"})

_NULL_SCHEMA = {"type": "null"}


def compact_input_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a leaner copy of a tool's input schema that accepts the same non-null arguments.

    - ``title`` keywords are dropped at every level (a property *named*
      ``title`` stays: the keys of ``properties`` and ``$defs`` are names).
    - A ``{"type": "null"}`` branch of ``anyOf`` / ``oneOf`` is dropped, and a
      union left with one branch is merged into its parent, so
      ``{"anyOf": [{"type": "integer"}, {"type": "null"}], "default": null}``
      becomes ``{"type": "integer"}``.
    - ``"default": null`` is dropped (leaving the field out means the same).

    Everything else is kept as it is: descriptions, non-null defaults, enums,
    bounds, ``required``. Literal values (``default``, ``enum``, ``const``,
    ``examples``) are never walked into. The input is not modified.
    """
    compacted: dict[str, Any] = _compact(schema)
    return compacted


def _compact(schema: Any) -> Any:
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key in _DROPPED_KEYWORDS:
            continue
        if key in _SCHEMA_MAPS and isinstance(value, dict):
            out[key] = {name: _compact(sub) for name, sub in value.items()}
        elif key in _SCHEMA_VALUES:
            out[key] = _compact(value)
        elif key in _SCHEMA_LISTS and isinstance(value, list):
            out[key] = [_compact(sub) for sub in value]
        else:
            out[key] = value
    for union in ("anyOf", "oneOf"):
        out = _drop_null_branch(out, union)
    if "default" in out and out["default"] is None:
        del out["default"]
    return out


def _drop_null_branch(schema: dict[str, Any], union: str) -> dict[str, Any]:
    """Drop the ``null`` branch of a union; merge a single remaining branch into the parent."""
    branches = schema.get(union)
    if not isinstance(branches, list) or _NULL_SCHEMA not in branches:
        return schema
    rest = [branch for branch in branches if branch != _NULL_SCHEMA]
    if not rest:
        return schema
    parent = {key: value for key, value in schema.items() if key != union}
    if len(rest) > 1:
        return {**parent, union: rest}
    branch = rest[0]
    if not isinstance(branch, dict) or set(branch) & set(parent):
        # A keyword on both sides would have to be combined, not merged: keep the union.
        return {**parent, union: rest}
    return {**branch, **parent}


class SpecivoMCPServer(MCPServer):
    """``MCPServer`` with text-only tool results and lean advertised input schemas."""

    def add_tool(
        self,
        fn: Callable[..., Any],
        name: str | None = None,
        title: str | None = None,
        description: str | None = None,
        annotations: ToolAnnotations | None = None,
        icons: list[Icon] | None = None,
        meta: dict[str, Any] | None = None,
        structured_output: bool | None = None,
    ) -> None:
        """Register a tool; unstructured (text content, no output schema) unless asked otherwise."""
        super().add_tool(
            fn,
            name=name,
            title=title,
            description=description,
            annotations=annotations,
            icons=icons,
            meta=meta,
            structured_output=False if structured_output is None else structured_output,
        )

    async def list_tools(self) -> list[MCPTool]:
        """List all tools with compacted input schemas (validation keeps the originals)."""
        return [
            tool.model_copy(update={"input_schema": compact_input_schema(tool.input_schema)})
            for tool in await super().list_tools()
        ]
