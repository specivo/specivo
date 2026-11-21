"""Structured nodes of the link graph: tags, metadata values, versions, sprints, assignees and hand-entered commits.

Covers the edges from items to these nodes, hub roots and their members, the
rule that a hub is expanded only as the root or when its kind is asked for,
degrees that count only what the reader may see, and root parsing.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import AnonymousAccessDeniedError, NotFoundError
from specivo.models.issue import Issue
from specivo.models.project import Project
from specivo.models.repository import IssueCommitLink, RepositoryCommit
from specivo.models.sprint import Sprint
from specivo.models.tag import Tag, TagLink
from specivo.models.user import User
from specivo.models.version import Version
from specivo.models.wiki import WikiPage
from specivo.services.anonymous_access_service import set_anonymous_access_enabled, set_anonymous_permissions
from specivo.services.anonymous_user_service import get_anonymous_user
from specivo.services.entity_link_service import ITEM_KINDS, LinkGraph, resolve_root
from tests.factories.user import UserFactory
from tests.fixtures.forge_hooks import make_integration
from tests.integration.link_graph_helpers import World, build_world, grant, issue, links, page

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

SHA_KNOWN = "a1b2c3d4e5f6" + "0" * 28
SHA_UNKNOWN = "f" * 40


@pytest.fixture
async def world(db_session: AsyncSession) -> World:
    return await build_world(db_session, "ELN")


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


async def _version(db: AsyncSession, project: Project, name: str, *items: Issue, sharing: str = "none") -> Version:
    version = Version(project_id=project.id, name=name, sharing=sharing)
    db.add(version)
    await db.flush()
    for item in items:
        item.fixed_version_id = version.id
    await db.commit()
    return version


async def _user(db: AsyncSession, login: str) -> User:
    user = UserFactory.build(login=login, status="active")
    db.add(user)
    await db.commit()
    return user


def _refs(graph: LinkGraph) -> set[str]:
    return {n.ref for n in graph.nodes}


def _by_ref(graph: LinkGraph) -> dict:
    return {n.ref: n for n in graph.nodes}


# -- edges from an item ------------------------------------------------------


async def test_an_issue_points_at_its_tags_version_sprint_assignee_component_and_commits(db_session, world):
    db = db_session
    key = world.home.key
    root = await issue(db, world, world.home, "Root")
    other = await issue(db, world, world.home, "Other")
    doc = await page(db, world, world.home, "Importer Notes")
    await _tag(db, world.home, "importer", root, other, doc)
    await _version(db, world.home, "0.13.0", root, other)
    sprint = Sprint(project_id=world.home.id, name="Sprint 7")
    db.add(sprint)
    await db.flush()
    root.sprint_id = sprint.id
    root.assigned_to_id = world.reader.id
    root.issue_metadata = {"component": "importer", "commits": [SHA_KNOWN, SHA_UNKNOWN.upper(), "not-a-sha"]}
    integration = await make_integration(db, world.home)
    commit = RepositoryCommit(integration_id=integration.id, sha=SHA_KNOWN, message="Import tags\n\nbody")
    db.add(commit)
    await db.flush()
    db.add(IssueCommitLink(commit_id=commit.id, issue_id=other.id, source="message"))
    await db.commit()

    graph = await links.get_links(db, world.reader, ("issue", root.id))
    nodes = _by_ref(graph)
    assert set(nodes) == {
        f"tag:{key}/importer",
        f"version:{key}/0.13.0",
        f"sprint:{key}/Sprint 7",
        f"user:{world.reader.login}",
        f"component:{key}/importer",
        f"commit:{SHA_KNOWN[:12]}",
        f"commit:{SHA_UNKNOWN[:12]}",
    }
    # Hubs are leaves with their degree; issues and pages behind them are not listed.
    assert nodes[f"tag:{key}/importer"].degree == 3
    assert nodes[f"version:{key}/0.13.0"].degree == 2
    assert nodes[f"sprint:{key}/Sprint 7"].degree == 1
    assert nodes[f"user:{world.reader.login}"].degree == 1
    assert nodes[f"component:{key}/importer"].degree == 1
    # The known SHA merged with the repository's commit (a visible issue links it); the unknown one stands alone.
    assert nodes[f"commit:{SHA_KNOWN[:12]}"].title == "Import tags"
    assert nodes[f"commit:{SHA_UNKNOWN[:12]}"].degree is None
    kinds = {(e.kind, e.target) for e in graph.edges}
    assert ("tagged", f"tag:{key}/importer") in kinds
    assert ("in_component", f"component:{key}/importer") in kinds
    assert ("targets_version", f"version:{key}/0.13.0") in kinds
    assert ("in_sprint", f"sprint:{key}/Sprint 7") in kinds
    assert ("assigned_to", f"user:{world.reader.login}") in kinds
    assert all(e.direction == "out" for e in graph.edges)
    # direction="in" never reaches the hubs an item points at.
    assert _refs(await links.get_links(db, world.reader, ("issue", root.id), direction="in")) == set()


async def test_item_kinds_leave_the_hubs_out(db_session, world):
    """The REST and web default reads only links between items."""
    root = await issue(db_session, world, world.home, "Root")
    await _tag(db_session, world.home, "backend", root)
    graph = await links.get_links(db_session, world.reader, ("issue", root.id), default_kinds=ITEM_KINDS)
    assert graph.nodes == []


async def test_other_metadata_keys_become_nodes_when_the_project_lists_them(db_session, world):
    db = db_session
    world.home.settings = {"graph_metadata_keys": ["team", "component", "bad/key"]}
    root = await issue(db, world, world.home, "Root")
    root.issue_metadata = {"team": ["backend", "search"], "component": "api", "bad/key": "x", "area": "ignored"}
    await db.commit()

    graph = await links.get_links(db, world.reader, ("issue", root.id))
    key = world.home.key
    assert _refs(graph) == {
        f"metadata:{key}/team/backend",
        f"metadata:{key}/team/search",
        f"component:{key}/api",
    }
    assert {e.kind for e in graph.edges} == {"in_metadata", "in_component"}
    team = await resolve_root(db, world.reader, f"metadata:{key}/team/backend", hubs=True)
    members = await links.get_links(db, world.reader, team)
    assert members.root is not None and members.root.title == "team: backend"
    assert _refs(members) == {root.display_key}


# -- hub roots ---------------------------------------------------------------


async def test_a_version_root_lists_its_issues_and_with_types_their_assignees_and_tags(db_session, world):
    db = db_session
    key = world.home.key
    first = await issue(db, world, world.home, "First")
    second = await issue(db, world, world.home, "Second")
    outside = await issue(db, world, world.home, "Not in the release")
    first.assigned_to_id = world.reader.id
    first.issue_metadata = {"component": "importer"}
    second.issue_metadata = {"component": "search"}
    await _version(db, world.home, "0.13.0", first, second)
    doc = await page(db, world, world.home, "Release Notes")
    await _tag(db, world.home, "release", first, doc)
    await db.commit()

    root = await resolve_root(db, world.reader, f"version:{key}/0.13.0", hubs=True)
    one_hop = await links.get_links(db, world.reader, root)
    assert one_hop.root is not None and one_hop.root.ref == f"version:{key}/0.13.0"
    assert one_hop.root.degree == 2
    assert _refs(one_hop) == {first.display_key, second.display_key}
    assert {(e.source, e.kind, e.direction) for e in one_hop.edges} == {
        (first.display_key, "targets_version", "in"),
        (second.display_key, "targets_version", "in"),
    }
    # Members point at the hub; there is nothing "out" of it.
    assert _refs(await links.get_links(db, world.reader, root, direction="out")) == set()

    # The root lists its members even when types leaves its own kind out.
    rich = await links.get_links(db, world.reader, root, depth=2, kinds=["assigned_to", "tagged", "in_component"])
    assert _refs(rich) == {
        first.display_key,
        second.display_key,
        f"user:{world.reader.login}",
        f"tag:{key}/release",
        f"component:{key}/importer",
        f"component:{key}/search",
    }
    assert outside.display_key not in _refs(rich)
    assert _by_ref(rich)[f"tag:{key}/release"].degree == 2  # the issue and the page

    # A tag root lists the issues and pages that carry it.
    tag_graph = await links.get_links(
        db, world.reader, await resolve_root(db, world.reader, f"tag:{key}/RELEASE", hubs=True)
    )
    assert _refs(tag_graph) == {first.display_key, f"wiki:{key}/release-notes"}
    assert doc.id

    # A component root lists the issues holding that value.
    component = await links.get_links(
        db, world.reader, await resolve_root(db, world.reader, f"component:{key}/search", hubs=True)
    )
    assert _refs(component) == {second.display_key}


async def test_a_hub_neighbour_is_expanded_only_when_its_kind_is_asked_for(db_session, world):
    db = db_session
    key = world.home.key
    root = await issue(db, world, world.home, "Root")
    peers = [await issue(db, world, world.home, f"Peer {i}") for i in range(3)]
    await _tag(db, world.home, "shared", root, *peers)

    default = await links.get_links(db, world.reader, ("issue", root.id), depth=2)
    assert _refs(default) == {f"tag:{key}/shared"}
    assert _by_ref(default)[f"tag:{key}/shared"].degree == 4

    asked = await links.get_links(db, world.reader, ("issue", root.id), depth=2, kinds=["tagged"])
    assert _refs(asked) == {f"tag:{key}/shared"} | {p.display_key for p in peers}
    # The root's own edge to the tag is listed once, not again from the tag's side.
    assert len([e for e in asked.edges if e.source == root.display_key]) == 1


async def test_a_hub_root_is_capped_at_the_node_limit_but_its_degree_counts_all(db_session, world):
    db = db_session
    members = [await issue(db, world, world.home, f"Member {i}") for i in range(5)]
    version = await _version(db, world.home, "2.0", *members)

    graph = await links.get_links(db, world.reader, ("version", version.id), limit=3)
    assert len(graph.nodes) == 3
    assert graph.truncated is True
    assert graph.root is not None and graph.root.degree == 5
    # Newest first.
    assert [n.ref for n in graph.nodes] == [m.display_key for m in reversed(members)][:3]


async def test_a_user_root_lists_the_issues_assigned_to_them(db_session, world):
    db = db_session
    assigned = await issue(db, world, world.home, "Assigned")
    hidden = await issue(db, world, world.hidden, "Assigned elsewhere")
    assigned.assigned_to_id = world.reader.id
    hidden.assigned_to_id = world.reader.id
    await db.commit()

    root = await resolve_root(db, world.reader, f"user:{world.reader.login.upper()}", hubs=True)
    graph = await links.get_links(db, world.reader, root)
    assert _refs(graph) == {assigned.display_key}
    assert graph.root is not None and graph.root.degree == 1
    admin_view = await links.get_links(db, world.admin, root)
    assert admin_view.root is not None and admin_view.root.degree == 2


# -- permissions -------------------------------------------------------------


async def test_degrees_and_members_count_only_what_the_reader_may_see(db_session, world):
    db = db_session
    key = world.home.key
    visible = await issue(db, world, world.home, "Visible")
    private = await issue(db, world, world.home, "Private", private=True)
    doc = await page(db, world, world.home, "Tagged Page")
    tag = await _tag(db, world.home, "mixed", visible, private, doc)
    version = await _version(db, world.home, "3.0", visible, private)

    # A reader who may view issues but not the wiki, and not private issues.
    issues_only = await _user(db, "eln_issues_only")
    await grant(db, issues_only, world.home, ["view_issues"])

    tag_graph = await links.get_links(db, issues_only, ("tag", tag.id))
    assert _refs(tag_graph) == {visible.display_key}
    assert tag_graph.root is not None and tag_graph.root.degree == 1
    leaf = _by_ref(await links.get_links(db, issues_only, ("issue", visible.id)))
    assert leaf[f"tag:{key}/mixed"].degree == 1
    assert leaf[f"version:{key}/3.0"].degree == 1
    assert "Private" not in str([n.title for n in tag_graph.nodes])

    admin_tag = await links.get_links(db, world.admin, ("tag", tag.id))
    assert admin_tag.root is not None and admin_tag.root.degree == 3
    admin_version = await links.get_links(db, world.admin, ("version", version.id))
    assert _refs(admin_version) == {visible.display_key, private.display_key}
    assert doc.id


async def test_a_hub_in_a_project_the_reader_cannot_see_is_left_out(db_session, world):
    db = db_session
    mine = await issue(db, world, world.home, "Mine")
    # A version of the hidden project shared with every project, and a hidden issue tagged the same way.
    shared = await _version(db, world.hidden, "shared-1", mine, sharing="system")
    secret = await issue(db, world, world.hidden, "Secret")
    secret.fixed_version_id = shared.id
    await db.commit()

    graph = await links.get_links(db, world.reader, ("issue", mine.id))
    assert graph.nodes == []
    assert graph.edges == []
    # Reaching the hidden hub directly lists nothing either.
    assert (await links.get_links(db, world.reader, ("version", shared.id))).nodes == []
    for ref in (f"version:{world.hidden.key}/shared-1", f"component:{world.hidden.key}/x"):
        with pytest.raises(NotFoundError):
            await resolve_root(db, world.reader, ref, hubs=True)


async def test_a_non_member_of_a_private_project_resolves_nothing(db_session, world):
    db = db_session
    await _tag(db, world.home, "ops")
    await _version(db, world.home, "1.0")
    outsider = await _user(db, "eln_outsider")
    for ref in (
        f"tag:{world.home.key}/ops",
        f"version:{world.home.key}/1.0",
        f"component:{world.home.key}/api",
        f"user:{world.reader.login}",
    ):
        with pytest.raises(NotFoundError):
            await resolve_root(db, outsider, ref, hubs=True)
    # The same refs resolve for a member.
    assert (await resolve_root(db, world.reader, f"tag:{world.home.key}/ops", hubs=True))[0] == "tag"
    # Everyone may start from themselves.
    assert await resolve_root(db, outsider, "user:eln_outsider", hubs=True) == ("user", outsider.id)


async def test_the_anonymous_user_sees_hubs_of_a_public_project_but_no_people(db_session, world):
    db = db_session
    world.home.is_public = True
    await set_anonymous_permissions(db, world.home, ["view_issues"], world.admin)
    await set_anonymous_access_enabled(db, True, world.admin, confirmed_projects=[world.home.key])
    await db.commit()
    anonymous = await get_anonymous_user(db)
    assert anonymous is not None

    root = await issue(db, world, world.home, "Public")
    root.assigned_to_id = world.reader.id
    await _version(db, world.home, "4.0", root)
    await db.commit()

    graph = await links.get_links(db, anonymous, ("issue", root.id))
    assert _refs(graph) == {f"version:{world.home.key}/4.0"}
    assert await resolve_root(db, anonymous, f"version:{world.home.key}/4.0", hubs=True) == (
        "version",
        root.fixed_version_id,
    )
    with pytest.raises(NotFoundError):
        await resolve_root(db, anonymous, f"user:{world.reader.login}", hubs=True)
    # As for wiki refs, a project the anonymous user may not reach asks for a sign-in.
    with pytest.raises(AnonymousAccessDeniedError):
        await resolve_root(db, anonymous, f"tag:{world.hidden.key}/x", hubs=True)


# -- root parsing ------------------------------------------------------------


async def test_hub_refs_resolve_and_bad_ones_are_refused(db_session, world):
    db = db_session
    key = world.home.key
    tag = await _tag(db, world.home, "Search")
    version = await _version(db, world.home, "0.13.0")
    sprint = Sprint(project_id=world.home.id, name="Sprint 1")
    db.add(sprint)
    await db.commit()

    assert await resolve_root(db, world.reader, f"tag:{key.lower()}/search", hubs=True) == ("tag", tag.id)
    assert await resolve_root(db, world.reader, f" Version:{key}/0.13.0 ", hubs=True) == ("version", version.id)
    assert await resolve_root(db, world.reader, f"sprint:{key}/Sprint 1", hubs=True) == ("sprint", sprint.id)
    assert await resolve_root(db, world.reader, f"component:{key}/a/b", hubs=True) == (
        "component",
        (world.home.id, "component", "a/b"),
    )
    assert await resolve_root(db, world.reader, f"metadata:{key}/component/api", hubs=True) == (
        "component",
        (world.home.id, "component", "api"),
    )
    for bad in ("tag:", f"tag:{key}", f"tag:{key}/", "version:/1.0", f"metadata:{key}/component", "user:"):
        with pytest.raises((ValueError, NotFoundError)):
            await resolve_root(db, world.reader, bad, hubs=True)
    for missing in (
        f"tag:{key}/nope",
        f"version:{key}/9.9",
        f"sprint:{key}/Sprint 2",
        f"metadata:{key}/team/backend",  # not a graph key of the project
        "tag:NOSUCH/x",
        "user:nobody-here",
    ):
        with pytest.raises(NotFoundError):
            await resolve_root(db, world.reader, missing, hubs=True)
    with pytest.raises(ValueError):
        await links.get_links(db, world.reader, ("tag", tag.id), kinds=["no_such_kind"])
    # Issue and page routes (REST, web) never resolve a hub.
    with pytest.raises(NotFoundError):
        await resolve_root(db, world.reader, f"user:{world.reader.login}")


async def test_a_hand_entered_sha_merges_only_with_a_commit_of_the_issues_own_project(db_session, world):
    db = db_session
    root = await issue(db, world, world.home, "Root")
    root.issue_metadata = {"commits": [SHA_KNOWN]}
    elsewhere = await make_integration(db, world.hidden, repo="example-org/hidden")
    db.add(RepositoryCommit(integration_id=elsewhere.id, sha=SHA_KNOWN, message="Hidden work"))
    await db.commit()

    graph = await links.get_links(db, world.reader, ("issue", root.id))
    assert [(n.ref, n.title) for n in graph.nodes] == [(f"commit:{SHA_KNOWN[:12]}", "")]
    # A short SHA matches a commit of the project's own repository by prefix, but the
    # commit is shown only where the reader may see it already: an issue they can see
    # links it. Linked to a private issue only, it stays a bare SHA.
    own = await make_integration(db, world.home)
    commit = RepositoryCommit(integration_id=own.id, sha=SHA_KNOWN, message="Own work")
    db.add(commit)
    secret = await issue(db, world, world.home, "Private work", private=True)
    await db.flush()
    db.add(IssueCommitLink(commit_id=commit.id, issue_id=secret.id, source="message"))
    root.issue_metadata = {"commits": [SHA_KNOWN[:9]]}
    await db.commit()
    graph = await links.get_links(db, world.reader, ("issue", root.id))
    # Only what was typed: the full SHA of a commit the reader may not see stays hidden.
    assert [(n.ref, n.title) for n in graph.nodes] == [(f"commit:{SHA_KNOWN[:9]}", "")]
    # Once a visible issue links it, the hand-entered SHA merges with the commit.
    shown = await issue(db, world, world.home, "Public work")
    db.add(IssueCommitLink(commit_id=commit.id, issue_id=shown.id, source="message"))
    await db.commit()
    graph = await links.get_links(db, world.reader, ("issue", root.id))
    assert [(n.ref, n.title) for n in graph.nodes] == [(f"commit:{SHA_KNOWN[:12]}", "Own work")]
