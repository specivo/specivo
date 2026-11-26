"""Every MCP tool carries explicit readOnlyHint, destructiveHint and idempotentHint.

Clients act on these hints without asking (Hermes replays only read-only tools after a
dropped session and gates everything else behind approval), so a missing or wrong hint
is a behaviour bug, not a documentation gap.
"""

from __future__ import annotations

import pytest
from mcp.types import ToolAnnotations

from specivo.mcp.annotations import TOOL_ANNOTATIONS
from specivo.mcp.docs import generate_setup_guide
from specivo.mcp.server import mcp

pytestmark = pytest.mark.unit

#: Name parts that mean a tool can change state.
_WRITE_WORDS = (
    "create",
    "update",
    "delete",
    "remove",
    "add",
    "append",
    "edit",
    "replace",
    "restore",
    "move",
    "log",
    "start",
    "complete",
    "skip",
    "timer",
    "tag",
    "metadata",
)
_READ_PREFIXES = ("specivo_list_", "specivo_show_", "specivo_read_", "specivo_get_")


def _tools() -> dict[str, ToolAnnotations | None]:
    return {tool.name: tool.annotations for tool in mcp._tool_manager.list_tools()}


def _words(name: str) -> set[str]:
    return set(name.removeprefix("specivo_").split("_"))


def test_tools_are_registered() -> None:
    assert len(_tools()) > 50


def test_every_tool_has_all_three_hints() -> None:
    incomplete = [
        name
        for name, hints in _tools().items()
        if hints is None
        or hints.read_only_hint is None
        or hints.destructive_hint is None
        or hints.idempotent_hint is None
        or hints.open_world_hint is None
    ]
    assert incomplete == []


def test_every_tool_uses_the_shared_table() -> None:
    differs = [name for name, hints in _tools().items() if hints != TOOL_ANNOTATIONS.get(name)]
    assert differs == []


def test_the_table_names_only_registered_tools() -> None:
    assert sorted(set(TOOL_ANNOTATIONS) - set(_tools())) == []


def test_read_tools_are_read_only() -> None:
    wrong = [
        name
        for name, hints in _tools().items()
        if name.startswith(_READ_PREFIXES) and not (hints and hints.read_only_hint is True)
    ]
    assert wrong == []


def test_read_only_tools_are_neither_destructive_nor_idempotent() -> None:
    wrong = [
        name
        for name, hints in _tools().items()
        if hints and hints.read_only_hint and (hints.destructive_hint or hints.idempotent_hint)
    ]
    assert wrong == []


def test_write_tools_are_not_read_only() -> None:
    wrong = [
        name
        for name, hints in _tools().items()
        if _words(name) & set(_WRITE_WORDS)
        and not name.startswith(_READ_PREFIXES)
        and (hints is None or hints.read_only_hint is not False)
    ]
    assert wrong == []


def test_delete_tools_are_destructive() -> None:
    wrong = [
        name
        for name, hints in _tools().items()
        if _words(name) & {"delete", "remove"} and not (hints and hints.destructive_hint is True)
    ]
    assert wrong == []


def test_no_tool_reaches_outside_the_instance() -> None:
    assert [name for name, hints in _tools().items() if hints and hints.open_world_hint] == []


@pytest.mark.parametrize(
    "name",
    ["specivo_whoami", "specivo_search", "specivo_ai_usage_report", "specivo_setup_guide"],
)
def test_known_reads_are_read_only(name: str) -> None:
    hints = _tools()[name]
    assert hints is not None and hints.read_only_hint is True


@pytest.mark.parametrize(
    "name",
    ["specivo_log_ai_usage", "specivo_log_time", "specivo_timer", "specivo_edit_description", "specivo_metadata"],
)
def test_repeatable_side_effects_are_not_idempotent(name: str) -> None:
    hints = _tools()[name]
    assert hints is not None and hints.idempotent_hint is False


async def test_hints_reach_the_tool_list() -> None:
    """The hints go out on the wire in camelCase, as clients read them."""
    listed = {tool.name: tool for tool in await mcp.list_tools()}
    assert len(listed) == len(_tools())
    wire = listed["specivo_show_issue"].model_dump(by_alias=True, exclude_none=True)
    assert wire["annotations"] == {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    }


def test_the_setup_guide_tells_clients_the_hints_are_reliable() -> None:
    hints = generate_setup_guide(fmt="generic").split("## Tool hints", 1)[1].split("\n## ", 1)[0]
    for word in ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"):
        assert word in hints
