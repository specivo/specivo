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

- **Short descriptions.** A tool registered with ``summary=`` is advertised
  with that summary and short parameter descriptions; the full text is its
  help (``specivo.mcp.tool_help``), returned by ``tool_help`` and appended to
  the error of a call whose arguments fail validation.

Only the advertised list changes. Arguments are validated against the tool's
original ``parameters`` (``Tool.parameters`` and the argument model built from
the function signature), so an explicit ``null`` is still accepted where the
function takes ``None``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, TypeVar

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.types import CallToolResult, Icon, InputRequiredResult, ToolAnnotations
from mcp.types import Tool as MCPTool
from pydantic import ValidationError

from specivo.mcp.tool_help import (
    LEAN,
    ToolDoc,
    full_help,
    lean_input_schema,
    parameter_help,
    short_description,
    tool_listing_var,
)

_CallableT = TypeVar("_CallableT", bound=Callable[..., Any])

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
    """``MCPServer`` with text-only tool results, lean input schemas and short descriptions."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._tool_docs: dict[str, ToolDoc] = {}

    def tool(
        self,
        name: str | None = None,
        title: str | None = None,
        description: str | None = None,
        annotations: ToolAnnotations | None = None,
        icons: list[Icon] | None = None,
        meta: dict[str, Any] | None = None,
        structured_output: bool | None = None,
        *,
        summary: str | None = None,
        params: Mapping[str, str] | None = None,
    ) -> Callable[[_CallableT], _CallableT]:
        """Register a tool; ``summary`` and ``params`` are its advertised short form.

        ``summary`` replaces the description in ``tools/list``; ``params`` maps a
        parameter name to its short description ("" for none). The docstring (or
        ``description``) and the ``Field`` descriptions stay the full help.
        """
        if callable(name):
            raise TypeError("Use @mcp.tool(...), not @mcp.tool")

        def decorator(fn: _CallableT) -> _CallableT:
            self.add_tool(
                fn,
                name=name,
                title=title,
                description=description,
                annotations=annotations,
                icons=icons,
                meta=meta,
                structured_output=structured_output,
            )
            if summary is not None:
                self._tool_docs[name or fn.__name__] = ToolDoc(summary=summary, params=dict(params or {}))
            return fn

        return decorator

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

    def tool_doc(self, name: str) -> ToolDoc | None:
        """The short form registered for tool *name*, if any."""
        return self._tool_docs.get(name)

    async def list_tools(self) -> list[MCPTool]:
        """List all tools with compacted input schemas (validation keeps the originals).

        In the ``LEAN`` listing (the default) a tool with a summary is advertised
        with it and with short parameter descriptions.
        """
        lean = tool_listing_var.get() == LEAN
        listed = []
        for tool in await super().list_tools():
            schema = compact_input_schema(tool.input_schema)
            doc = self._tool_docs.get(tool.name) if lean else None
            if doc is None:
                listed.append(tool.model_copy(update={"input_schema": schema}))
                continue
            listed.append(
                tool.model_copy(
                    update={
                        "description": short_description(tool.name, doc),
                        "input_schema": lean_input_schema(schema, doc),
                    }
                )
            )
        return listed

    def tool_help(self, name: str) -> str | None:
        """The full help of tool *name* (toolset, description, parameters), or ``None`` if unknown."""
        tool = self._tool_manager.get_tool(name)
        if tool is None:
            return None
        return full_help(tool.name, tool.description, compact_input_schema(tool.parameters))

    async def call_tool(
        self, name: str, arguments: dict[str, Any], context: Context[Any, Any] | None = None
    ) -> CallToolResult | InputRequiredResult:
        """Call a tool; arguments that fail validation get the tool's full parameter docs appended.

        A model that guessed an argument from the short listing learns the
        details where it needs them, without a help call. Every other error
        keeps its message.
        """
        try:
            return await super().call_tool(name, arguments, context)
        except ToolError as exc:
            if isinstance(exc, UnexpectedToolError) or not isinstance(exc.__cause__, ValidationError):
                raise
            if tool_listing_var.get() != LEAN:
                raise
            tool = self._tool_manager.get_tool(name)
            if tool is None:
                raise
            docs = parameter_help(tool.name, compact_input_schema(tool.parameters))
            # Pydantic's "For further information visit <url>" lines only cost tokens.
            message = "\n".join(line for line in str(exc).splitlines() if "errors.pydantic.dev" not in line)
            raise ToolError(f"{message}\n\n{docs}") from exc.__cause__
