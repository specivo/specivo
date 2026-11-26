"""The size of ``tools/list`` stays under a budget.

Clients without a tool search of their own put the whole answer into the prompt on
every turn, so it must not grow back unnoticed. The budgets sit a little above the
measured size; raise one only together with a reason in the commit message.

Measured as the JSON of the ``tools/list`` result a client receives over the
in-process transport (the same middleware as over HTTP). Tools are listed with
short descriptions (``specivo.mcp.tool_help``); each description and each
parameter description has a maximum too, and every tool's full help is
reachable (``tests/unit/test_mcp_tool_help.py`` checks its content).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from mcp import Client

from specivo.mcp.server import mcp
from specivo.mcp.tool_help import PARAM_DESCRIPTION_MAX, TOOL_DESCRIPTION_MAX
from specivo.mcp.toolsets import CORE, TOOLSETS, mcp_toolsets_var, tools_in

pytestmark = pytest.mark.unit

#: Characters of the tools/list JSON for the default connection (core).
CORE_BUDGET = 33_000
#: Characters of the tools/list JSON with ?toolsets=all.
ALL_BUDGET = 71_000


async def _listed(toolsets: frozenset[str] | None) -> list[dict[str, Any]]:
    token = mcp_toolsets_var.set(toolsets)
    try:
        async with Client(mcp) as client:
            result = await client.list_tools()
    finally:
        mcp_toolsets_var.reset(token)
    return [tool.model_dump(by_alias=True, mode="json", exclude_none=True) for tool in result.tools]


def _size(tools: list[dict[str, Any]]) -> int:
    return len(json.dumps({"tools": tools}))


async def test_the_default_connection_lists_core_within_budget() -> None:
    tools = await _listed(None)
    assert {tool["name"] for tool in tools} == set(TOOLSETS[CORE])
    assert _size(tools) <= CORE_BUDGET, _size(tools)


async def test_all_toolsets_stay_within_budget() -> None:
    tools = await _listed(frozenset(TOOLSETS))
    assert {tool["name"] for tool in tools} == tools_in(TOOLSETS)
    assert _size(tools) <= ALL_BUDGET, _size(tools)


async def test_the_listing_has_no_titles_output_schemas_or_null_branches() -> None:
    tools = await _listed(frozenset(TOOLSETS))
    for tool in tools:
        assert "outputSchema" not in tool, tool["name"]
        text = json.dumps(tool["inputSchema"])
        assert '"title": "' not in text.replace('"title": {"', ""), tool["name"]
        assert '{"type": "null"}' not in text, tool["name"]
        assert '"default": null' not in text, tool["name"]


async def test_calling_a_hidden_tool_names_the_toolset_to_enable() -> None:
    token = mcp_toolsets_var.set(None)
    try:
        async with Client(mcp) as client:
            result = await client.call_tool("specivo_create_sprint", {"project_key": "ACME", "name": "S1"})
    finally:
        mcp_toolsets_var.reset(token)
    assert result.is_error is True
    text = result.content[0].text  # type: ignore[union-attr]
    assert text.startswith("specivo_create_sprint is in the 'planning' toolset")
    assert "/mcp/?toolsets=core,planning" in text


async def test_every_description_is_short() -> None:
    tools = await _listed(frozenset(TOOLSETS))
    too_long = [tool["name"] for tool in tools if len(tool.get("description", "")) > TOOL_DESCRIPTION_MAX]
    assert too_long == []
    for tool in tools:
        assert tool.get("description"), tool["name"]


async def test_every_parameter_description_is_short() -> None:
    tools = await _listed(frozenset(TOOLSETS))
    too_long = [
        f"{tool['name']}.{name}"
        for tool in tools
        for name, prop in tool["inputSchema"].get("properties", {}).items()
        if len(prop.get("description", "")) > PARAM_DESCRIPTION_MAX
    ]
    assert too_long == []


async def test_every_tool_has_full_help() -> None:
    tools = await _listed(frozenset(TOOLSETS))
    async with Client(mcp) as client:
        for tool in tools:
            text = mcp.tool_help(tool["name"])
            assert text and len(text) > len(tool["description"]) // 2, tool["name"]
            resource = await client.read_resource(f"specivo://docs/tools/{tool['name']}")
            assert resource.contents[0].text == text  # type: ignore[union-attr]
