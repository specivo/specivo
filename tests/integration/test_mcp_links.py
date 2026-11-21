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


async def _attach(db_session: AsyncSession, world: World, item, name: str) -> None:
    from specivo.models.attachment import Attachment

    container = "Issue" if hasattr(item, "display_key") else "WikiPage"
    db_session.add(
        Attachment(
            container_type=container,
            container_id=item.id,
            filename=name,
            disk_filename=f"emc-{name}",
            filesize=1,
            author_id=world.admin.id,
        )
    )
    await db_session.commit()


async def test_get_links_counts_attachments_instead_of_listing_them(db_session, world):
    doc = await page(db_session, world, world.home, "Spec", "")
    root = await issue(db_session, world, world.home, "Root", "[[Spec]]")
    await rebuild(db_session, root)
    for name in ("a.png", "b.png"):
        await _attach(db_session, world, root, name)
    await _attach(db_session, world, doc, "c.pdf")

    data = json.loads(await _get_links(db_session, world.reader, root.display_key))
    assert data["root"]["attachments"] == 2
    assert [(n["ref"], n["attachments"]) for n in data["nodes"]] == [(wiki_ref(world.home, "spec"), 1)]
    assert all(e["kind"] != "attachment" for e in data["edges"])

    listed = json.loads(await _get_links(db_session, world.reader, root.display_key, types=["attachment"]))
    assert sorted(n["title"] for n in listed["nodes"]) == ["a.png", "b.png"]


async def test_get_links_from_a_version_with_its_assignees_and_tags(db_session, world):
    from specivo.models.tag import Tag, TagLink
    from specivo.models.version import Version

    key = world.home.key
    first = await issue(db_session, world, world.home, "First")
    second = await issue(db_session, world, world.home, "Second")
    secret = await issue(db_session, world, world.hidden, "Secret")
    doc = await page(db_session, world, world.home, "Guide", "")
    version = Version(project_id=world.home.id, name="0.13.0", sharing="system")
    tag = Tag(project_id=world.home.id, name="importer")
    db_session.add_all([version, tag])
    await db_session.flush()
    for item in (first, second, secret):
        item.fixed_version_id = version.id
    first.assigned_to_id = world.reader.id
    first.issue_metadata = {"component": "importer"}
    db_session.add_all([TagLink(tag_id=tag.id, issue_id=first.id), TagLink(tag_id=tag.id, wiki_page_id=doc.id)])
    await db_session.commit()

    data = json.loads(
        await _get_links(
            db_session,
            world.reader,
            f"version:{key}/0.13.0",
            depth=2,
            types=["targets_version", "assigned_to", "tagged", "in_component"],
        )
    )
    assert data["root"]["ref"] == f"version:{key}/0.13.0"
    assert data["root"]["degree"] == 2
    nodes = {n["ref"]: n for n in data["nodes"]}
    assert set(nodes) == {
        first.display_key,
        second.display_key,
        f"user:{world.reader.login}",
        f"tag:{key}/importer",
        f"component:{key}/importer",
    }
    assert nodes[f"tag:{key}/importer"]["degree"] == 2
    assert "Secret" not in json.dumps(data)

    tagged = json.loads(await _get_links(db_session, world.reader, f"tag:{key}/importer"))
    assert {n["ref"] for n in tagged["nodes"]} == {first.display_key, wiki_ref(world.home, "guide")}

    # The inline lines of show_issue stay free of hubs.
    shown = await _show_issue(db_session, world.reader, first.display_key, metadata_only=True)
    assert f"tag:{key}/importer" not in shown
    assert f"version:{key}/0.13.0" not in shown


def test_the_tool_documents_structured_nodes():
    guide = generate_setup_guide(fmt="generic", mcp_server=server.mcp)
    assert "version:ACME/<name>" in guide
    doc = server.specivo_get_links.__doc__ or ""
    assert "degree" in doc and "attachments" in doc
