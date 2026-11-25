"""MCP toolsets: the mapping, the ``toolsets`` URL parameter and ``ToolsetMiddleware``."""

from __future__ import annotations

from collections import Counter
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS, CallToolResult
from starlette.datastructures import QueryParams

from specivo.mcp.docs import generate_setup_guide
from specivo.mcp.server import mcp
from specivo.mcp.toolsets import (
    ADMIN,
    AI_SPEND,
    CORE,
    DEFAULT_TOOLSETS,
    PLANNING,
    RECURRING,
    TOOLSETS,
    ToolsetMiddleware,
    UnknownToolsetError,
    format_toolsets,
    hidden_tool_message,
    is_tool_enabled,
    mcp_toolsets_var,
    parse_toolsets,
    tools_in,
    toolset_of,
    toolsets_from_request_context,
)

pytestmark = pytest.mark.unit


def _registered() -> set[str]:
    return {tool.name for tool in mcp._tool_manager.list_tools()}


# ---------------------------------------------------------------------------
# The mapping
# ---------------------------------------------------------------------------


def test_every_registered_tool_is_in_exactly_one_toolset() -> None:
    counts = Counter(tool for tools in TOOLSETS.values() for tool in tools)
    assert [tool for tool, n in counts.items() if n > 1] == []
    assert sorted(_registered() - set(counts)) == []
    assert sorted(set(counts) - _registered()) == []


def test_the_toolsets_are_the_documented_ones() -> None:
    assert list(TOOLSETS) == [CORE, PLANNING, RECURRING, ADMIN, AI_SPEND]
    assert DEFAULT_TOOLSETS == {CORE}


@pytest.mark.parametrize(
    ("tool", "toolset"),
    [
        # Everything the Hermes plugin calls is core.
        ("specivo_agent_session", CORE),
        ("specivo_update_issue", CORE),
        ("specivo_timer", CORE),
        ("specivo_create_issue", CORE),
        ("specivo_list_issues", CORE),
        ("specivo_list_lookups", CORE),
        ("specivo_whoami", CORE),
        ("specivo_add_comment", CORE),
        # Issue writes take fixed_version_id / sprint_id: their lists stay in core.
        ("specivo_list_versions", CORE),
        ("specivo_list_sprints", CORE),
        ("specivo_create_sprint", PLANNING),
        ("specivo_delete_version", PLANNING),
        ("specivo_create_recurring_pattern", RECURRING),
        ("specivo_create_tag", ADMIN),
        ("specivo_delete_metadata_schema", ADMIN),
        ("specivo_log_ai_usage", AI_SPEND),
        ("specivo_ai_usage_report", AI_SPEND),
    ],
)
def test_toolset_of(tool: str, toolset: str) -> None:
    assert toolset_of(tool) == toolset


def test_every_ai_tool_is_in_ai_spend() -> None:
    ai_tools = {name for name in _registered() if name.startswith("specivo_ai_") or "ai_usage" in name}
    assert ai_tools == set(TOOLSETS[AI_SPEND])


def test_a_tool_in_no_toolset_is_never_hidden() -> None:
    assert is_tool_enabled("plugin_tool", frozenset({CORE})) is True
    assert is_tool_enabled("specivo_create_sprint", frozenset({CORE})) is False
    assert is_tool_enabled("specivo_create_sprint", frozenset({CORE, PLANNING})) is True


# ---------------------------------------------------------------------------
# Parsing the URL parameter
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, {CORE}),
        ("", {CORE}),
        ("  ", {CORE}),
        ("core", {CORE}),
        ("core,ai-spend", {CORE, AI_SPEND}),
        (" Core , PLANNING ,", {CORE, PLANNING}),
        ("planning", {PLANNING}),
        ("all", set(TOOLSETS)),
        ("core,all", set(TOOLSETS)),
    ],
)
def test_parse_toolsets(value: str | None, expected: set[str]) -> None:
    assert parse_toolsets(value) == expected


def test_an_unknown_toolset_names_the_valid_ones() -> None:
    with pytest.raises(UnknownToolsetError) as exc:
        parse_toolsets("core,sprints,bogus")
    message = str(exc.value)
    assert "'bogus', 'sprints'" in message
    for name in (*TOOLSETS, "all"):
        assert name in message


def test_format_toolsets_is_canonical() -> None:
    assert format_toolsets({AI_SPEND, CORE}) == "core,ai-spend"
    assert format_toolsets(set(TOOLSETS)) == "all"


def test_tools_in() -> None:
    assert tools_in({CORE}) == set(TOOLSETS[CORE])
    assert tools_in(TOOLSETS) == _registered()


def test_the_hidden_tool_message_names_the_toolset_to_add() -> None:
    message = hidden_tool_message("specivo_create_sprint", frozenset({CORE, AI_SPEND}))
    assert message.startswith("specivo_create_sprint is in the 'planning' toolset")
    assert "/mcp/?toolsets=core,planning,ai-spend" in message
    assert "?toolsets=all" in message


# ---------------------------------------------------------------------------
# Reading the toolsets of a request
# ---------------------------------------------------------------------------


def _ctx(
    method: str, query: str | None = None, params: dict[str, Any] | None = None, request_id: int | None = 1
) -> Any:
    request = None if query is None else SimpleNamespace(query_params=QueryParams(query))
    return SimpleNamespace(method=method, params=params, request_id=request_id, request=request)


def test_the_request_parameter_wins_over_the_context() -> None:
    token = mcp_toolsets_var.set(frozenset({PLANNING}))
    try:
        assert toolsets_from_request_context(_ctx("tools/list", "toolsets=ai-spend&session_id=abc")) == {AI_SPEND}
        # Without the parameter (an in-process client sets the context instead).
        assert toolsets_from_request_context(_ctx("tools/list")) == {PLANNING}
    finally:
        mcp_toolsets_var.reset(token)


def test_without_a_parameter_or_a_request_the_default_applies() -> None:
    assert toolsets_from_request_context(_ctx("tools/list", "")) == DEFAULT_TOOLSETS
    assert toolsets_from_request_context(_ctx("tools/list")) == DEFAULT_TOOLSETS


# ---------------------------------------------------------------------------
# The middleware
# ---------------------------------------------------------------------------


def _listing(*names: str) -> dict[str, Any]:
    return {"tools": [{"name": name, "inputSchema": {"type": "object"}} for name in names], "nextCursor": "x"}


async def test_tools_list_is_filtered() -> None:
    async def call_next(_ctx: Any) -> dict[str, Any]:
        return _listing("specivo_whoami", "specivo_create_sprint", "specivo_log_ai_usage", "plugin_tool")

    result = await ToolsetMiddleware()(_ctx("tools/list", "toolsets=core,ai-spend"), call_next)
    assert [tool["name"] for tool in result["tools"]] == ["specivo_whoami", "specivo_log_ai_usage", "plugin_tool"]
    assert result["nextCursor"] == "x"


async def test_calling_a_hidden_tool_answers_a_tool_error_without_running_it() -> None:
    async def call_next(_ctx: Any) -> dict[str, Any]:
        raise AssertionError("the tool must not run")

    result = await ToolsetMiddleware()(
        _ctx("tools/call", "", params={"name": "specivo_create_sprint", "arguments": {}}), call_next
    )
    assert isinstance(result, CallToolResult)
    assert result.is_error is True
    assert "connect with ?toolsets=core,planning" in result.content[0].text.lower()  # type: ignore[union-attr]


async def test_calling_an_enabled_tool_passes_through() -> None:
    reached: list[str] = []

    async def call_next(ctx: Any) -> dict[str, Any]:
        reached.append(ctx.params["name"])
        return {"content": [], "isError": False}

    await ToolsetMiddleware()(
        _ctx("tools/call", "toolsets=planning", params={"name": "specivo_create_sprint"}), call_next
    )
    assert reached == ["specivo_create_sprint"]


async def test_an_unknown_toolset_refuses_the_request() -> None:
    async def call_next(_ctx: Any) -> dict[str, Any]:
        raise AssertionError("must not be reached")

    with pytest.raises(MCPError) as exc:
        await ToolsetMiddleware()(_ctx("initialize", "toolsets=bogus"), call_next)
    assert exc.value.error.code == INVALID_PARAMS
    assert "Unknown MCP toolset 'bogus'" in exc.value.error.message


async def test_notifications_pass_untouched() -> None:
    async def call_next(_ctx: Any) -> None:
        return None

    assert (
        await ToolsetMiddleware()(_ctx("notifications/initialized", "toolsets=bogus", request_id=None), call_next)
        is None
    )


def test_the_setup_guide_explains_toolsets() -> None:
    guide = generate_setup_guide(fmt="generic", mcp_server=mcp)
    assert "## Toolsets" in guide
    assert "?toolsets=core,ai-spend" in guide
    for name in TOOLSETS:
        assert f"**{name}**" in guide
    assert "`specivo_create_sprint`" in guide.split("## Toolsets", 1)[1].split("## Standard workflows", 1)[0]
