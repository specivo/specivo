"""Short tool descriptions in ``tools/list``, the full help on demand.

Every tool is advertised with a summary and short parameter descriptions; its
full description and parameter docs are reachable through
``specivo_setup_guide(tool=...)``, the ``specivo://docs/tools/{name}`` resource,
and the error of a call whose arguments fail validation.
"""

from __future__ import annotations

import logging
import typing
from collections.abc import Iterator
from typing import Any

import pytest
from mcp import Client

from specivo.mcp.ai_billing_account_tools import AI_BILLING_ACCOUNT_OPS
from specivo.mcp.ai_reimbursement_claim_tools import AI_REIMBURSEMENT_CLAIM_OPS
from specivo.mcp.ai_spend_budget_tools import AI_SPEND_BUDGET_OPS
from specivo.mcp.ai_subscription_tools import AI_SUBSCRIPTION_OPS
from specivo.mcp.ai_usage_tools import AI_USAGE_OPS
from specivo.mcp.compact_server import compact_input_schema
from specivo.mcp.server import mcp
from specivo.mcp.tool_help import (
    COMMON_PARAMS,
    FULL,
    help_pointer,
    needs_override,
    short_param_description,
    tool_listing_var,
)
from specivo.mcp.tools.agent_sessions import AGENT_SESSION_OPS
from specivo.mcp.tools.time_tracking import _TIMER_OPS
from specivo.mcp.toolsets import TOOLSETS, mcp_toolsets_var, toolset_of
from specivo.schemas.relation import VALID_RELATION_TYPES
from specivo.services.link_suggestion_service import OPS as LINK_SUGGESTION_OPS

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _quiet_sdk_logs(caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    caplog.set_level(logging.WARNING, logger="mcp.server.mcpserver.server")
    yield


@pytest.fixture
def all_toolsets() -> Iterator[None]:
    token = mcp_toolsets_var.set(frozenset(TOOLSETS))
    yield
    mcp_toolsets_var.reset(token)


def _registered() -> list[Any]:
    return list(mcp._tool_manager.list_tools())


def test_every_tool_has_a_summary() -> None:
    assert [tool.name for tool in _registered() if mcp.tool_doc(tool.name) is None] == []


def test_no_parameter_needs_an_explicit_short_description() -> None:
    """A parameter whose full text and first sentence are both too long needs ``params=`` on its tool."""
    missing = []
    for tool in _registered():
        doc = mcp.tool_doc(tool.name)
        assert doc is not None
        for name, prop in compact_input_schema(tool.parameters).get("properties", {}).items():
            if needs_override(name, prop.get("description"), doc.params):
                missing.append(f"{tool.name}.{name}")
    assert missing == []


def test_overrides_name_real_parameters() -> None:
    stale = []
    for tool in _registered():
        doc = mcp.tool_doc(tool.name)
        assert doc is not None
        stale += [f"{tool.name}.{name}" for name in doc.params if name not in tool.parameters["properties"]]
    assert stale == []


def test_short_parameter_descriptions_prefer_overrides_then_common_text() -> None:
    long_text = "First sentence that is short. " + "More detail. " * 20
    assert short_param_description("x", long_text, {"x": "Own."}) == "Own."
    assert short_param_description("x", long_text, {"x": ""}) is None
    assert short_param_description("project_key", long_text, {}) == COMMON_PARAMS["project_key"]
    assert short_param_description("limit", "Max issues (1-100).", {}) is None
    assert short_param_description("x", "Short text.", {}) == "Short text."
    assert short_param_description("x", long_text, {}) == "First sentence that is short."
    assert short_param_description("x", None, {}) is None


async def test_the_listing_advertises_summaries_with_a_help_pointer(all_toolsets: None) -> None:
    async with Client(mcp) as client:
        tools = (await client.list_tools()).tools
    assert len(tools) == len(_registered())
    for tool in tools:
        doc = mcp.tool_doc(tool.name)
        assert doc is not None
        assert tool.description == f"{doc.summary} {help_pointer(tool.name)}"


async def test_the_full_listing_is_the_original_text(all_toolsets: None) -> None:
    token = tool_listing_var.set(FULL)
    try:
        async with Client(mcp) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    finally:
        tool_listing_var.reset(token)
    for registered in _registered():
        listed = tools[registered.name]
        assert listed.description == registered.description
        original = compact_input_schema(registered.parameters)
        assert listed.input_schema == original


async def test_every_tool_has_full_help_from_the_guide_and_the_resource() -> None:
    async with Client(mcp) as client:
        for tool in _registered():
            result = await client.call_tool("specivo_setup_guide", {"tool": tool.name})
            assert result.is_error is False, tool.name
            text = result.content[0].text  # type: ignore[union-attr]
            resource = await client.read_resource(f"specivo://docs/tools/{tool.name}")
            assert resource.contents[0].text == text  # type: ignore[union-attr]

            assert text.startswith(f"# {tool.name} (toolset: {toolset_of(tool.name)})")
            first_line = tool.description.strip().splitlines()[0].strip()
            assert first_line in text
            for name, prop in tool.parameters.get("properties", {}).items():
                assert f"- `{name}` (" in text, (tool.name, name)
                full = " ".join(str(prop.get("description", "")).split())
                assert full in text, (tool.name, name)


async def test_help_covers_tools_outside_the_connection_toolsets() -> None:
    async with Client(mcp) as client:
        result = await client.call_tool("specivo_setup_guide", {"tool": "specivo_create_sprint"})
    assert result.is_error is False
    assert result.content[0].text.startswith("# specivo_create_sprint (toolset: planning)")  # type: ignore[union-attr]


async def test_an_unknown_tool_name_lists_the_known_tools() -> None:
    async with Client(mcp) as client:
        result = await client.call_tool("specivo_setup_guide", {"tool": "specivo_nope"})
    assert result.is_error is True
    text = result.content[0].text  # type: ignore[union-attr]
    assert "Unknown tool 'specivo_nope'" in text
    assert "specivo_create_issue" in text


async def test_a_missing_argument_teaches_the_parameters() -> None:
    async with Client(mcp) as client:
        result = await client.call_tool("specivo_create_issue", {"project_key": "ACME", "subject": "Broken"})
    assert result.is_error is True
    text = result.content[0].text  # type: ignore[union-attr]
    assert text.startswith("Error executing tool specivo_create_issue: 1 validation error")
    assert "tracker_id" in text
    assert "Parameters of specivo_create_issue:" in text
    assert "- `tracker_id` (integer; required): Tracker ID from specivo_list_lookups" in text
    assert "errors.pydantic.dev" not in text


async def test_a_value_outside_the_enum_teaches_the_choices() -> None:
    async with Client(mcp) as client:
        result = await client.call_tool("specivo_metadata", {"target_ref": "ACME-1", "key": "k", "op": "frobnicate"})
    assert result.is_error is True
    text = result.content[0].text  # type: ignore[union-attr]
    assert "Parameters of specivo_metadata:" in text
    assert '`op` (one of "set", "get", "delete", "append", "remove"; required)' in text


async def test_the_full_listing_does_not_append_help_to_errors() -> None:
    token = tool_listing_var.set(FULL)
    try:
        async with Client(mcp) as client:
            result = await client.call_tool("specivo_create_issue", {"project_key": "ACME", "subject": "Broken"})
    finally:
        tool_listing_var.reset(token)
    assert result.is_error is True
    assert "Parameters of" not in result.content[0].text  # type: ignore[union-attr]


def _literal(tool: str, param: str) -> set[str]:
    fn = mcp._tool_manager.get_tool(tool).fn  # type: ignore[union-attr]
    hint = typing.get_type_hints(fn, include_extras=True)[param]
    while typing.get_origin(hint) is typing.Annotated:
        hint = typing.get_args(hint)[0]
    return {arg for arg in typing.get_args(hint) if isinstance(arg, str)}


@pytest.mark.parametrize(
    ("tool", "param", "values"),
    [
        ("specivo_agent_session", "op", AGENT_SESSION_OPS),
        ("specivo_timer", "op", _TIMER_OPS),
        ("specivo_link_suggestion", "op", LINK_SUGGESTION_OPS),
        ("specivo_add_relation", "relation_type", VALID_RELATION_TYPES),
        ("specivo_ai_usage", "op", AI_USAGE_OPS),
        ("specivo_ai_subscriptions", "op", AI_SUBSCRIPTION_OPS),
        ("specivo_ai_billing_accounts", "op", AI_BILLING_ACCOUNT_OPS),
        ("specivo_ai_reimbursement_claims", "op", AI_REIMBURSEMENT_CLAIM_OPS),
        ("specivo_ai_spend_budgets", "op", AI_SPEND_BUDGET_OPS),
    ],
)
def test_enum_arguments_match_what_the_tool_accepts(tool: str, param: str, values: Any) -> None:
    """The ``Literal`` in the signature (the schema's ``enum``) lists exactly the values the tool handles."""
    assert _literal(tool, param) == set(values)
