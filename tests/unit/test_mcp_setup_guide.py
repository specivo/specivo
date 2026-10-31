"""The agent setup guide names every MCP tool and teaches the tag workflow."""

from __future__ import annotations

import pytest

from specivo.mcp.docs import generate_setup_guide
from specivo.mcp.server import mcp

_TAG_TOOLS = ("specivo_tag", "specivo_list_tags", "specivo_create_tag", "specivo_update_tag", "specivo_delete_tag")


def _registered_tools() -> list[str]:
    return [tool.name for tool in mcp._tool_manager.list_tools()]


def test_the_guide_names_every_registered_tool() -> None:
    """Built without the server's fallback rows, so a new tool must get a written summary."""
    guide = generate_setup_guide(fmt="generic")
    missing = [name for name in _registered_tools() if f"| `{name}` |" not in guide]
    assert missing == []


def test_the_guide_lists_no_tool_that_does_not_exist() -> None:
    registered = set(_registered_tools())
    guide = generate_setup_guide(fmt="generic")
    rows = [line.split("`")[1] for line in guide.splitlines() if line.startswith("| `specivo_")]
    assert [name for name in rows if name not in registered] == []


@pytest.mark.parametrize("fmt", ["claude", "cursor", "generic"])
def test_the_guide_teaches_tags(fmt: str) -> None:
    guide = generate_setup_guide(fmt=fmt, mcp_server=mcp)
    for name in _TAG_TOOLS:
        assert f"| `{name}` |" in guide
    assert "### Tags" in guide
    # Reuse the vocabulary: add creates missing names, so typos become new tags.
    assert "specivo_list_tags(project_key)" in guide
    assert 'specivo_tag("ACME-12", op="add"' in guide
    assert "wiki:ACME/<slug>" in guide
    assert "tags=[" in guide


def test_the_guide_forbids_tags_in_the_body() -> None:
    guide = generate_setup_guide(fmt="generic")
    anti_patterns = guide.split("## Anti-patterns", 1)[1]
    assert "**Tags**:" in anti_patterns
    assert "frontmatter" in anti_patterns


def test_the_guide_teaches_the_timer() -> None:
    guide = generate_setup_guide(fmt="generic", mcp_server=mcp)
    assert "| `specivo_timer` |" in guide
    assert "### Time work with the timer" in guide
    for op in ("start", "get", "stop", "discard"):
        assert f'op="{op}"' in guide


@pytest.mark.parametrize("fmt", ["claude", "cursor", "generic"])
def test_the_guide_teaches_mermaid_diagrams(fmt: str) -> None:
    guide = generate_setup_guide(fmt=fmt, mcp_server=mcp)
    assert "### Diagrams (Mermaid)" in guide
    assert "```mermaid" in guide


def test_the_connect_instructions_mention_mermaid() -> None:
    """Clients show the server instructions before any tool call, so diagrams are known up front."""
    assert "```mermaid" in (mcp.instructions or "")
