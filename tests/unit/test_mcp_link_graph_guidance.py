"""Agents are told what the link graph is, where to find it, and how to connect things.

- ``specivo_search`` and ``specivo_show_issue`` name the link graph and its
  tool, ``specivo_get_links``;
- the setup guide prefers relations for issue to issue, and no longer tells
  agents to write keys into text so the graph finds them.
"""

from __future__ import annotations

import pytest

from specivo.mcp.docs import generate_setup_guide
from specivo.mcp.server import mcp

pytestmark = [pytest.mark.unit]


def _description(name: str) -> str:
    tool = next(tool for tool in mcp._tool_manager.list_tools() if tool.name == name)
    return tool.description or ""


@pytest.mark.parametrize("name", ["specivo_search", "specivo_show_issue"])
def test_the_tool_points_to_the_link_graph(name: str) -> None:
    description = _description(name)
    assert "link graph" in description
    assert "specivo_get_links" in description


@pytest.mark.parametrize("fmt", ["claude", "cursor", "generic"])
def test_the_guide_prefers_relations_and_drops_the_old_advice(fmt: str) -> None:
    guide = generate_setup_guide(fmt=fmt, mcp_server=mcp)
    assert "so the graph finds them" not in guide
    assert "link graph" in guide
    assert "specivo_add_relation" in guide
    assert "naturally refers" in guide
    assert "suggest" not in guide.lower().split("### connecting issues and pages")[1].split("###")[0]
