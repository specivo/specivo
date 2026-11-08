"""The link graph over MCP: ``specivo_get_links`` and the link lines of show_issue / read_wiki."""

from __future__ import annotations

import json

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.mcp import server
from specivo.mcp.docs import generate_setup_guide
from specivo.mcp.tools import _get_links, _read_wiki, _show_issue
from specivo.models.relation import IssueRelation
from tests.integration.link_graph_helpers import World, build_world, comment, issue, page, rebuild, wiki_ref

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.serial]


@pytest.fixture
async def world(db_session: AsyncSession) -> World:
    return await build_world(db_session, "EMC")


async def test_get_links_returns_filtered_json(db_session, world):
    secret = await issue(db_session, world, world.hidden, "Classified")
    doc = await page(db_session, world, world.home, "Architecture", "")
    root = await issue(db_session, world, world.home, "Root", f"[[Architecture]] {secret.display_key}")
    await rebuild(db_session, root)

    data = json.loads(await _get_links(db_session, world.reader, root.display_key))
    assert data["root"]["ref"] == root.display_key
    assert [n["ref"] for n in data["nodes"]] == [wiki_ref(world.home, "architecture")]
    assert "Classified" not in json.dumps(data)

    back = json.loads(await _get_links(db_session, world.reader, wiki_ref(world.home, "architecture"), direction="in"))
    assert [(e["source"], e["kind"], e["direction"]) for e in back["edges"]] == [(root.display_key, "wiki_link", "in")]
    only = json.loads(await _get_links(db_session, world.reader, root.display_key, types=["relation"]))
    assert only["edges"] == []
    assert doc.id


async def test_get_links_refuses_what_the_reader_cannot_see(db_session, world):
    from specivo.core.exceptions import NotFoundError

    secret = await issue(db_session, world, world.hidden, "Hidden root")
    with pytest.raises(NotFoundError):
        await _get_links(db_session, world.reader, secret.display_key)
    with pytest.raises(ValueError):
        await _get_links(db_session, world.reader, "wiki:nope")


async def test_show_issue_and_read_wiki_list_links_inline(db_session, world):
    doc = await page(db_session, world, world.home, "Runbook", "")
    other = await issue(db_session, world, world.home, "Blocked one")
    root = await issue(db_session, world, world.home, "Root", "Follow [[Runbook]] and [[Missing Page]]")
    db_session.add(IssueRelation(issue_from_id=root.id, issue_to_id=other.id, relation_type="blocks"))
    mentioner = await issue(db_session, world, world.home, "Mentions root")
    note = await comment(db_session, world, mentioner, f"Waiting for {root.display_key}")
    await rebuild(db_session, root, note)

    shown = await _show_issue(db_session, world.reader, root.display_key, metadata_only=True)
    assert f"Links to: {wiki_ref(world.home, 'runbook')} Runbook" in shown
    assert f"Linked from: {mentioner.display_key} Mentions root" in shown
    assert f"(comment {mentioner.display_key}#{note.sequence})" in shown
    assert f"Related: blocks {other.display_key} Blocked one" in shown
    assert f"Broken links: {wiki_ref(world.home, 'missing-page')}" in shown

    read = await _read_wiki(db_session, world.reader, world.home.key, "runbook", metadata_only=True)
    assert f"Linked from: {root.display_key} Root" in read
    assert doc.id


async def test_inline_lines_are_capped(db_session, world, monkeypatch):
    monkeypatch.setattr("specivo.core.constants.ENTITY_LINKS_INLINE_LIMIT", 2)
    root = await page(db_session, world, world.home, "Hub", " ".join(f"[[Spoke {i}]]" for i in range(4)))
    for i in range(4):
        await page(db_session, world, world.home, f"Spoke {i}", "")
    await rebuild(db_session, root)
    read = await _read_wiki(db_session, world.reader, world.home.key, "hub", metadata_only=True)
    assert "; ... and 2 more" in read


def test_the_tool_is_registered_and_announced():
    guide = generate_setup_guide(fmt="generic", mcp_server=server.mcp)
    assert "specivo_get_links" in guide
    assert server.specivo_get_links.__doc__ and "backlinks" in server.specivo_get_links.__doc__
