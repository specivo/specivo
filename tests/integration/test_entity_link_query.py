"""Queries over the link graph: path finding, filters after the read, compact text, search context, hub depth 3.

The permission tests follow a path: a link that exists only through an issue
the reader may not see is never used, and nothing in any output names it.
"""

from __future__ import annotations

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.issue import Issue
from specivo.models.lookups import IssueStatus, Tracker
from specivo.models.project import Project
from specivo.models.relation import IssueRelation
from specivo.models.tag import Tag, TagLink
from specivo.models.user import User
from specivo.models.version import Version
from specivo.models.wiki import WikiPage
from specivo.services import entity_link_query
from specivo.services.entity_link_query import (
    BUDGET_EXHAUSTED,
    NO_PATH,
    apply_filter,
    cap_nodes,
    compact_graph,
    compact_path,
    expand_hits,
    find_path,
    parse_filter,
)
from specivo.services.entity_link_service import resolve_root
from tests.factories.user import UserFactory
from tests.integration.link_graph_helpers import World, build_world, comment, grant, issue, links, page, rebuild

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]


@pytest.fixture
async def world(db_session: AsyncSession) -> World:
    return await build_world(db_session, "ELQ")


def _relate(db: AsyncSession, a: Issue, b: Issue, kind: str = "relates") -> None:
    db.add(IssueRelation(issue_from_id=a.id, issue_to_id=b.id, relation_type=kind))


async def _version(db: AsyncSession, project: Project, name: str, *items: Issue) -> Version:
    version = Version(project_id=project.id, name=name, sharing="none")
    db.add(version)
    await db.flush()
    for item in items:
        item.fixed_version_id = version.id
    await db.commit()
    return version


async def _tag(db: AsyncSession, project: Project, name: str, *items: Issue | WikiPage) -> Tag:
    tag = Tag(project_id=project.id, name=name)
    db.add(tag)
    await db.flush()
    for item in items:
        if isinstance(item, Issue):
            db.add(TagLink(tag_id=tag.id, issue_id=item.id))
        else:
            db.add(TagLink(tag_id=tag.id, wiki_page_id=item.id))
    await db.commit()
    return tag


async def _assignee(db: AsyncSession, project: Project, login: str, *assigned: Issue) -> User:
    """A member of *project* with the reader's role, assigned *assigned*: private issues are visible to them."""
    user = UserFactory.build(login=login, status="active")
    db.add(user)
    await db.flush()
    await grant(db, user, project, ["view_issues", "view_wiki"])
    for item in assigned:
        item.assigned_to_id = user.id
    await db.commit()
    return user


class _Queries:
    """Counts the SQL statements run while it is active."""

    def __init__(self, db: AsyncSession) -> None:
        self.engine = db.bind.sync_engine  # type: ignore[union-attr]
        self.count = 0

    def _record(self, *_args: object) -> None:
        self.count += 1

    def __enter__(self) -> _Queries:
        event.listen(self.engine, "before_cursor_execute", self._record)
        return self

    def __exit__(self, *_exc: object) -> None:
        event.remove(self.engine, "before_cursor_execute", self._record)


# -- path finding ---------------------------------------------------------------


async def test_shortest_path_runs_over_relations_text_links_and_parents(db_session, world):
    db = db_session
    start = await issue(db, world, world.home, "Start")
    middle = await issue(db, world, world.home, "Middle")
    doc = await page(db, world, world.home, "Design Notes")
    end = await issue(db, world, world.home, "End", "Documented in [[Design Notes]]")
    child = await issue(db, world, world.home, "Child of end")
    child.parent_id = end.id
    _relate(db, start, middle, "blocks")
    await db.commit()
    note = await comment(db, world, middle, "See [[Design Notes]] for the plan")
    await rebuild(db, end, note)

    path = await find_path(db, world.reader, ("issue", start.id), ("issue", child.id))
    data = path.as_dict()
    assert data["reason"] is None
    assert [n["ref"] for n in data["path"]["nodes"]] == [
        start.display_key,
        middle.display_key,
        f"wiki:{world.home.key}/design-notes",
        end.display_key,
        child.display_key,
    ]
    assert data["path"]["hops"] == 4
    edges = [(e["source"], e["kind"], e["target"]) for e in data["path"]["edges"]]
    assert edges == [
        (start.display_key, "relation", middle.display_key),
        (middle.display_key, "wiki_link", f"wiki:{world.home.key}/design-notes"),
        (end.display_key, "wiki_link", f"wiki:{world.home.key}/design-notes"),
        (child.display_key, "parent", end.display_key),
    ]
    assert data["path"]["edges"][1]["via"] == f"{middle.display_key}#{note.sequence}"
    assert doc.id

    text = compact_path(path)
    lines = text.splitlines()
    assert lines[0] == f"path: {start.display_key} -> {child.display_key}, 4 hops"
    first = f'{start.display_key} "Start" [ELQ New] -relation:blocks-> {middle.display_key} "Middle" [ELQ New]'
    assert lines[1] == first


async def test_path_to_itself_has_no_hops(db_session, world):
    start = await issue(db_session, world, world.home, "Alone")
    data = (await find_path(db_session, world.reader, ("issue", start.id), ("issue", start.id))).as_dict()
    assert data["path"]["hops"] == 0
    assert [n["ref"] for n in data["path"]["nodes"]] == [start.display_key]


async def test_no_path_within_four_hops(db_session, world):
    db = db_session
    chain = [await issue(db, world, world.home, f"Chain {i}") for i in range(6)]
    for a, b in zip(chain, chain[1:], strict=False):
        _relate(db, a, b)
    await db.commit()
    near = await find_path(db, world.reader, ("issue", chain[0].id), ("issue", chain[4].id))
    assert near.as_dict()["path"]["hops"] == 4
    far = await find_path(db, world.reader, ("issue", chain[0].id), ("issue", chain[5].id))
    assert far.as_dict()["path"] is None
    assert far.reason == NO_PATH
    assert far.root is not None and far.to is not None
    assert compact_path(far).startswith(f"path: {chain[0].display_key} -> {chain[5].display_key}: none (")


async def test_a_path_through_a_private_issue_is_found_only_by_a_reader_who_may_see_it(db_session, world):
    db = db_session
    start = await issue(db, world, world.home, "Public start")
    secret = await issue(db, world, world.home, "Secret middle", private=True)
    end = await issue(db, world, world.home, "Public end")
    _relate(db, start, secret)
    _relate(db, secret, end)
    await db.commit()
    member = await _assignee(db, world.home, "elq_member", secret)

    outsider = await find_path(db, world.reader, ("issue", start.id), ("issue", end.id))
    assert outsider.reason == NO_PATH
    dumped = str(outsider.as_dict()) + compact_path(outsider)
    assert "Secret" not in dumped and secret.display_key not in dumped

    found = await find_path(db, member, ("issue", start.id), ("issue", end.id))
    assert [n.ref for n in found.nodes or []] == [start.display_key, secret.display_key, end.display_key]


async def test_a_path_never_runs_through_a_project_the_reader_cannot_see(db_session, world):
    db = db_session
    start = await issue(db, world, world.home, "Home start")
    hidden = await issue(db, world, world.hidden, "Hidden bridge")
    end = await issue(db, world, world.home, "Home end")
    _relate(db, start, hidden)
    _relate(db, hidden, end)
    await db.commit()
    outsider = await find_path(db, world.reader, ("issue", start.id), ("issue", end.id))
    assert outsider.reason == NO_PATH
    assert "Hidden bridge" not in str(outsider.as_dict())
    admin = await find_path(db, world.admin, ("issue", start.id), ("issue", end.id))
    assert admin.nodes is not None and len(admin.nodes) == 3


async def test_hubs_are_stepped_through_only_when_types_names_their_kind(db_session, world):
    db = db_session
    first = await issue(db, world, world.home, "First tagged")
    second = await issue(db, world, world.home, "Second tagged")
    tag = await _tag(db, world.home, "storage", first, second)

    plain = await find_path(db, world.reader, ("issue", first.id), ("issue", second.id))
    assert plain.reason == NO_PATH
    tagged = await find_path(db, world.reader, ("issue", first.id), ("issue", second.id), types=["tagged"])
    assert [n.ref for n in tagged.nodes or []] == [
        first.display_key,
        f"tag:{world.home.key}/storage",
        second.display_key,
    ]
    # A hub may always be an end of the path.
    to_hub = await find_path(db, world.reader, ("issue", first.id), ("tag", tag.id))
    assert [e.kind for e in to_hub.edges or []] == ["tagged"]
    with pytest.raises(ValueError, match="Unknown link kind"):
        await find_path(db, world.reader, ("issue", first.id), ("tag", tag.id), types=["bogus"])
    with pytest.raises(ValueError, match="types names none"):
        await find_path(db, world.reader, ("issue", first.id), ("tag", tag.id), types=["attachment"])


async def test_a_wide_hub_exhausts_the_budget_in_a_bounded_number_of_queries(db_session, world, monkeypatch):
    db = db_session
    monkeypatch.setattr(entity_link_query, "ENTITY_LINKS_PATH_NODE_BUDGET", 6)
    members = [await issue(db, world, world.home, f"Member {i}") for i in range(12)]
    version = await _version(db, world.home, "9.9", *members)
    wide = await issue(db, world, world.home, "Wide")
    for other in members:
        _relate(db, wide, other)
    goal = await issue(db, world, world.home, "Unreachable goal")
    await db.commit()

    with _Queries(db) as queries:
        result = await find_path(db, world.reader, ("version", version.id), ("issue", goal.id))
    assert result.reason == BUDGET_EXHAUSTED
    assert result.explored <= 6 + 1
    assert queries.count <= 40

    with _Queries(db) as queries:
        result = await find_path(db, world.reader, ("issue", wide.id), ("issue", goal.id))
    assert result.reason == BUDGET_EXHAUSTED
    assert queries.count <= 40


# -- filters ----------------------------------------------------------------------


async def test_a_version_root_filtered_by_component_returns_exactly_the_matching_issues(db_session, world):
    db = db_session
    key = world.home.key
    importer = await issue(db, world, world.home, "Importer work")
    importer_two = await issue(db, world, world.home, "More importer work")
    search = await issue(db, world, world.home, "Search work")
    loose = await issue(db, world, world.home, "Importer, other version")
    importer.issue_metadata = {"component": "importer"}
    importer_two.issue_metadata = {"component": ["Importer", "api"]}
    search.issue_metadata = {"component": "search"}
    loose.issue_metadata = {"component": "importer"}
    await _version(db, world.home, "0.14.0", importer, importer_two, search)

    root = await resolve_root(db, world.reader, f"version:{key}/0.14.0", hubs=True)
    graph = await links.get_links(db, world.reader, root)
    await apply_filter(db, world.reader, graph, parse_filter({"component": "importer"}))
    assert graph.root is not None and graph.root.ref == f"version:{key}/0.14.0"
    assert {n.ref for n in graph.nodes} == {importer.display_key, importer_two.display_key}
    assert {(e.source, e.target) for e in graph.edges} == {
        (importer.display_key, f"version:{key}/0.14.0"),
        (importer_two.display_key, f"version:{key}/0.14.0"),
    }


async def test_filter_on_status_tracker_assignee_and_version(db_session, world):
    db = db_session
    closed = IssueStatus(name="ELQ Closed", position=9, category="closed")
    tracker = Tracker(name="ELQ Bug", default_status_id=None, position=3)
    db.add_all([closed, tracker])
    await db.flush()
    root = await issue(db, world, world.home, "Root")
    done = await issue(db, world, world.home, "Done one")
    mine = await issue(db, world, world.home, "Assigned to the reader")
    bug = await issue(db, world, world.home, "A bug")
    done.status_id = closed.id
    mine.assigned_to_id = world.reader.id
    bug.tracker_id = tracker.id
    for other in (done, mine, bug):
        _relate(db, root, other)
    await _version(db, world.home, "1.0", mine)
    await db.commit()

    async def kept(flt: dict) -> set[str]:
        graph = await links.get_links(db, world.reader, ("issue", root.id), kinds=["relation"])
        await apply_filter(db, world.reader, graph, parse_filter(flt))
        return {n.ref for n in graph.nodes}

    assert await kept({"status": "closed"}) == {done.display_key}
    assert await kept({"status": "open"}) == {mine.display_key, bug.display_key}
    assert await kept({"status": "elq closed"}) == {done.display_key}
    assert await kept({"tracker": "ELQ Bug"}) == {bug.display_key}
    assert await kept({"assignee": "me"}) == {mine.display_key}
    assert await kept({"assignee": world.reader.login.upper()}) == {mine.display_key}
    assert await kept({"version": "1.0"}) == {mine.display_key}
    assert await kept({"status": ["closed", "ELQ New"], "tracker": "elq bug"}) == {bug.display_key}


async def test_filter_drops_pages_and_keeps_hubs_joined_to_kept_issues(db_session, world):
    db = db_session
    key = world.home.key
    doc = await page(db, world, world.home, "Overview")
    root = await issue(db, world, world.home, "Root", "See [[Overview]] and [[Nowhere]]")
    keep = await issue(db, world, world.home, "Open match")
    keep.issue_metadata = {"component": "api"}
    drop = await issue(db, world, world.home, "Other component")
    drop.issue_metadata = {"component": "ui"}
    _relate(db, root, keep)
    _relate(db, root, drop)
    await _tag(db, world.home, "root-tag", root)
    await rebuild(db, root)

    graph = await links.get_links(db, world.reader, ("issue", root.id), depth=2)
    refs = {n.ref for n in graph.nodes}
    assert {f"wiki:{key}/overview", f"component:{key}/ui", f"component:{key}/api"} <= refs
    await apply_filter(db, world.reader, graph, parse_filter({"component": "api"}))
    assert {n.ref for n in graph.nodes} == {keep.display_key, f"component:{key}/api", f"tag:{key}/root-tag"}
    assert not any(e.broken for e in graph.edges)
    assert doc.id


async def test_filter_keys_and_values_are_checked(db_session, world):
    root = await issue(db_session, world, world.home, "Root")
    graph = await links.get_links(db_session, world.reader, ("issue", root.id))
    with pytest.raises(ValueError, match="Unknown filter key"):
        await apply_filter(db_session, world.reader, graph, parse_filter({"team": "x"}))
    with pytest.raises(ValueError, match="non-empty string"):
        parse_filter({"status": 3})
    with pytest.raises(ValueError, match="non-empty string"):
        parse_filter({"status": []})
    with pytest.raises(ValueError, match="at most"):
        parse_filter({f"k{i}": "v" for i in range(11)})
    assert parse_filter(None) == {}
    world.home.settings = {"graph_metadata_keys": ["team"]}
    other = await issue(db_session, world, world.home, "Team member")
    other.issue_metadata = {"team": "backend"}
    _relate(db_session, root, other)
    await db_session.commit()
    graph = await links.get_links(db_session, world.reader, ("issue", root.id))
    await apply_filter(db_session, world.reader, graph, parse_filter({"team": "Backend"}))
    assert [n.ref for n in graph.nodes] == [other.display_key]


async def test_cap_nodes_cuts_and_marks_truncated(db_session, world):
    db = db_session
    root = await issue(db, world, world.home, "Root")
    for i in range(3):
        _relate(db, root, await issue(db, world, world.home, f"Neighbour {i}"))
    await db.commit()
    graph = await links.get_links(db, world.reader, ("issue", root.id))
    cap_nodes(graph, 2)
    assert len(graph.nodes) == 2 and len(graph.edges) == 2 and graph.truncated


# -- compact text -------------------------------------------------------------------


async def test_compact_graph_writes_one_line_per_edge_and_each_title_once(db_session, world):
    db = db_session
    key = world.home.key
    await page(db, world, world.home, "Runbook")
    root = await issue(db, world, world.home, "Root", "Follow [[Runbook]] and [[Missing Page]]")
    blocker = await issue(db, world, world.home, "Blocker")
    _relate(db, blocker, root, "blocks")
    mentioner = await issue(db, world, world.home, 'Mentions "root"', f"Waits for {root.display_key}")
    await rebuild(db, mentioner)
    await _tag(db, world.home, "ops", root)
    await rebuild(db, root)

    graph = await links.get_links(db, world.reader, ("issue", root.id))
    lines = compact_graph(graph).splitlines()
    assert lines[0] == f'root: {root.display_key} "Root" [ELQ New] ({len(graph.nodes)} nodes, {len(graph.edges)} edges)'
    assert f'{root.display_key} -wiki_link-> wiki:{key}/runbook "Runbook"' in lines
    assert f"{root.display_key} -wiki_link-> wiki:{key}/missing-page (broken)" in lines
    assert f"{root.display_key} -tagged-> tag:{key}/ops (degree 1)" in lines
    assert f'{root.display_key} -relation:blocked-> {blocker.display_key} "Blocker" [ELQ New]' in lines
    assert f"{mentioner.display_key} \"Mentions 'root'\" [ELQ New] -mention-> {root.display_key}" in lines
    assert len(lines) == 1 + len(graph.edges)

    graph.truncated = True
    assert compact_graph(graph).splitlines()[0].endswith("edges, truncated)")


# -- hub depth 3 ------------------------------------------------------------------------


async def test_a_hub_root_reaches_pages_through_tags_at_depth_three(db_session, world):
    db = db_session
    key = world.home.key
    member = await issue(db, world, world.home, "In the release")
    doc = await page(db, world, world.home, "Storage Design")
    await _tag(db, world.home, "storage", member, doc)
    await _version(db, world.home, "2.0", member)

    root = await resolve_root(db, world.reader, f"version:{key}/2.0", hubs=True)
    graph = await links.get_links(db, world.reader, root, depth=3, kinds=["tagged"])
    assert {n.ref for n in graph.nodes} == {member.display_key, f"tag:{key}/storage", f"wiki:{key}/storage-design"}
    with pytest.raises(ValueError, match="only for a tag"):
        await links.get_links(db, world.reader, ("issue", member.id), depth=3)
    with pytest.raises(ValueError, match="depth must be"):
        await links.get_links(db, world.reader, root, depth=4)


# -- search context ---------------------------------------------------------------------


async def test_expand_hits_reads_the_first_five_issue_and_page_hits_only(db_session, world):
    from specivo.schemas.search import SearchResult

    db = db_session
    items = [await issue(db, world, world.home, f"Hit {i}") for i in range(7)]
    secret = await issue(db, world, world.hidden, "Hidden neighbour")
    _relate(db, items[0], secret)
    _relate(db, items[0], items[6])
    doc = await page(db, world, world.home, "Hit page")
    await db.commit()

    def hit(result_type: str, item_id: int) -> SearchResult:
        return SearchResult(result_type=result_type, id=item_id, title="t", score=1.0, project_key=world.home.key)

    hits = [hit("comment", 1), hit("wiki", doc.id), *(hit("issue", i.id) for i in items)]
    context = await expand_hits(db, world.reader, hits)
    assert sorted(context) == [1, 2, 3, 4, 5]
    first = context[2]
    assert first.root is not None and first.root.ref == items[0].display_key
    assert [n.ref for n in first.nodes] == [items[6].display_key]
    assert "Hidden" not in str(first.as_dict())
