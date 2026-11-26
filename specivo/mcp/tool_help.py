"""Short tool descriptions in ``tools/list``, the full help on demand.

A client without a tool search of its own puts every listed tool into the
model's prompt on every turn, so ``tools/list`` advertises a short form:

- **The tool description** is the tool's *summary* (``ToolDoc.summary``,
  given as ``summary=`` on ``@mcp.tool`` next to the tool): what it does,
  what to call first, and its hard constraints, ending with a pointer to the
  full help.
- **Parameter descriptions** are a few words: an explicit override
  (``params=`` on ``@mcp.tool``), else the shared text of a common parameter
  (``COMMON_PARAMS``, so ``project_key`` is described the same short way
  everywhere), else the full text when it is short enough, else its first
  sentence. ``tests/unit/test_mcp_tools_list_budget.py`` fails when a long
  description would need an override.

The full docstring (or ``description=``) and the full ``Field`` descriptions
stay the source of truth. They are what ``full_help`` renders, and agents
reach it three ways: ``specivo_setup_guide(tool=...)``, the MCP resource
``specivo://docs/tools/{name}``, and the error of a call whose arguments fail
validation, which carries the tool's full parameter docs.

``tool_listing_var`` switches the listing back to the full text (the
evaluation harness in ``scripts/mcp_eval`` compares both on the same code).
"""

from __future__ import annotations

import contextvars
import json
import re
import textwrap
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from specivo.mcp.toolsets import toolset_of

ToolListing = Literal["lean", "full"]
#: ``tools/list`` with summaries and short parameter descriptions.
LEAN: ToolListing = "lean"
#: ``tools/list`` with the full descriptions (what was advertised before summaries).
FULL: ToolListing = "full"

#: Which listing this connection gets; the default is ``LEAN``.
tool_listing_var: contextvars.ContextVar[ToolListing] = contextvars.ContextVar("mcp_tool_listing", default=LEAN)

#: Longest advertised tool description (summary plus the help pointer).
TOOL_DESCRIPTION_MAX = 360
#: Longest advertised parameter description.
PARAM_DESCRIPTION_MAX = 90

#: Short descriptions of parameters that mean the same in every tool. An empty
#: string advertises no description (the name and the schema say it all). A tool
#: whose parameter means something else overrides it with ``params=``.
COMMON_PARAMS: dict[str, str] = {
    "project_key": "Project key, e.g. ACME.",
    "issue_ref": "Issue key, e.g. ACME-12 (never a numeric id).",
    "slug": "Wiki page slug or title, e.g. 'Release Process'.",
    "offset": "",
    "limit": "",
}

_SENTENCE_END = re.compile(r"(?<=[.!?])\s")


@dataclass(frozen=True)
class ToolDoc:
    """The advertised short form of a tool: its summary and parameter overrides."""

    summary: str
    params: Mapping[str, str] = field(default_factory=dict)


def help_pointer(name: str) -> str:
    """The pointer that ends a short description."""
    return f"Details: specivo_setup_guide(tool='{name}')"


def short_description(name: str, doc: ToolDoc) -> str:
    """The advertised description of tool *name*: its summary and the help pointer."""
    return f"{doc.summary.strip()} {help_pointer(name)}"


def _first_sentence(text: str) -> str:
    return _SENTENCE_END.split(text.strip(), maxsplit=1)[0]


def short_param_description(name: str, full: str | None, overrides: Mapping[str, str]) -> str | None:
    """The advertised description of parameter *name*, or ``None`` for none.

    An override wins, then ``COMMON_PARAMS``, then the full text when it fits
    ``PARAM_DESCRIPTION_MAX``, then its first sentence. A text that still does
    not fit is cut at a word (``needs_override`` reports it).
    """
    if name in overrides:
        return overrides[name] or None
    if name in COMMON_PARAMS:
        return COMMON_PARAMS[name] or None
    if not full:
        return None
    full = " ".join(full.split())
    if len(full) <= PARAM_DESCRIPTION_MAX:
        return full
    first = _first_sentence(full)
    if len(first) <= PARAM_DESCRIPTION_MAX:
        return first
    return textwrap.shorten(full, PARAM_DESCRIPTION_MAX, placeholder="...")


def needs_override(name: str, full: str | None, overrides: Mapping[str, str]) -> bool:
    """Whether the short description of *name* had to be cut (an override is missing)."""
    if name in overrides or name in COMMON_PARAMS or not full:
        return False
    full = " ".join(full.split())
    return len(full) > PARAM_DESCRIPTION_MAX and len(_first_sentence(full)) > PARAM_DESCRIPTION_MAX


def lean_input_schema(schema: dict[str, Any], doc: ToolDoc) -> dict[str, Any]:
    """A copy of a (compacted) input schema with short parameter descriptions.

    Only the top-level properties change; nested definitions keep their text.
    """
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return schema
    lean: dict[str, Any] = {}
    for name, prop in properties.items():
        if not isinstance(prop, dict):
            lean[name] = prop
            continue
        short = short_param_description(name, prop.get("description"), doc.params)
        rest = {key: value for key, value in prop.items() if key != "description"}
        lean[name] = {**rest, "description": short} if short else rest
    return {**schema, "properties": lean}


# ---------------------------------------------------------------------------
# Full help
# ---------------------------------------------------------------------------


def _type_of(prop: dict[str, Any]) -> str:
    """A short human type for a JSON schema property."""
    if "enum" in prop:
        return "one of " + ", ".join(json.dumps(value) for value in prop["enum"])
    if "$ref" in prop:
        return str(prop["$ref"]).rsplit("/", 1)[-1]
    for union in ("anyOf", "oneOf"):
        if isinstance(prop.get(union), list):
            return " | ".join(_type_of(branch) for branch in prop[union] if isinstance(branch, dict))
    kind = prop.get("type")
    if kind == "array" and isinstance(prop.get("items"), dict):
        return f"array of {_type_of(prop['items'])}"
    if isinstance(kind, list):
        return " | ".join(str(part) for part in kind)
    return str(kind) if kind else "any"


def _bounds(prop: dict[str, Any]) -> list[str]:
    notes = []
    if "minimum" in prop:
        notes.append(f">= {prop['minimum']}")
    if "maximum" in prop:
        notes.append(f"<= {prop['maximum']}")
    if "default" in prop:
        notes.append(f"default {json.dumps(prop['default'])}")
    return notes


def _parameter_lines(schema: dict[str, Any]) -> list[str]:
    required = set(schema.get("required", []))
    lines = []
    for name, prop in (schema.get("properties") or {}).items():
        if not isinstance(prop, dict):
            continue
        notes = [_type_of(prop), "required" if name in required else "optional", *_bounds(prop)]
        description = " ".join(str(prop.get("description", "")).split())
        lines.append(f"- `{name}` ({'; '.join(notes)})" + (f": {description}" if description else ""))
    return lines


def parameter_help(name: str, schema: dict[str, Any]) -> str:
    """The full parameter docs of tool *name* from its (compacted) input schema."""
    lines = _parameter_lines(schema)
    if not lines:
        return f"{name} takes no arguments."
    sections = [f"Parameters of {name}:", *lines]
    for definition, sub in (schema.get("$defs") or {}).items():
        if isinstance(sub, dict):
            sections += ["", f"{definition} fields:", *_parameter_lines(sub)]
    return "\n".join(sections)


def full_help(name: str, description: str, schema: dict[str, Any]) -> str:
    """The full help of tool *name*: its toolset, full description and full parameter docs."""
    toolset = toolset_of(name)
    header = f"# {name}" + (f" (toolset: {toolset})" if toolset else "")
    body = _dedent_after_first_line(description).strip()
    return f"{header}\n\n{body}\n\n{parameter_help(name, schema)}\n"


def _dedent_after_first_line(text: str) -> str:
    """Docstrings indent every line but the first; dedent them as one block."""
    first, _, rest = text.partition("\n")
    return first.strip() + "\n" + textwrap.dedent(rest) if rest else first
