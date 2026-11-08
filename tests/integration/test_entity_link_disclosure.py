"""The link graph must not reveal what exists where the reader cannot look.

- A link to a missing target is shown only where the reader could see the
  target. Otherwise a reader without access to project B could compare a link
  to the existing (hidden, so left out) B-5 with one to the missing B-999
  (which would show as broken), and learn which of B's issues exist. A key of
  an unknown project is never stored, so the same comparison would also tell
  them that B exists at all.
- The settings stats (counts and unresolved targets) only cover links whose
  target project the viewer could see, for the same reason.
- An admin's project-limited API key gets no admin reach outside its projects:
  no private notes, no files on private notes.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.attachment import Attachment
from specivo.models.user import User
from specivo.services.api_key_scope import ProjectScope, bound_scope
from specivo.services.link_graph_admin_service import project_stats
from tests.factories.user import UserFactory
from tests.integration.link_graph_helpers import World, build_world, comment, grant, issue, links, page, rebuild

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]


@pytest.fixture
async def world(db_session: AsyncSession) -> World:
    return await build_world(db_session, "ELD")


@pytest.fixture
async def manager(db_session: AsyncSession, world: World) -> User:
    user = UserFactory.build(login="eld_manager", status="active")
    db_session.add(user)
    await db_session.commit()
    await grant(db_session, user, world.home, ["manage_project", "view_issues", "view_wiki"])
    return user


def _shape(graph) -> list[tuple]:
    """What a reader learns from a graph, with the refs that differ between the probes masked out."""
    return [(e.kind, e.direction, e.broken) for e in graph.edges] + [(n.type,) for n in graph.nodes]


async def test_existing_and_missing_hidden_issues_look_the_same(db_session, world):
    existing = await issue(db_session, world, world.hidden, "Private plan")
    await page(db_session, world, world.hidden, "Hidden Page")
    probe_existing = await issue(
        db_session,
        world,
        world.home,
        "Probe existing",
        f"{existing.display_key} [p](/projects/{world.hidden.key}/wiki/hidden-page/)",
    )
    probe_missing = await issue(
        db_session,
        world,
        world.home,
        "Probe missing",
        f"{world.hidden.key}-999 [p](/projects/{world.hidden.key}/wiki/no-such-page/)",
    )
    probe_unknown = await issue(db_session, world, world.home, "Probe unknown", "QZXV-1 [p](/projects/QZXV/wiki/x/)")
    await rebuild(db_session, probe_existing, probe_missing, probe_unknown)

    graphs = [
        await links.get_links(db_session, world.reader, ("issue", p.id))
        for p in (probe_existing, probe_missing, probe_unknown)
    ]
    assert [_shape(g) for g in graphs] == [[], [], []]

    # Someone who may see the project's issues and wiki is shown the missing targets.
    admin_view = await links.get_links(db_session, world.admin, ("issue", probe_missing.id))
    assert {e.target for e in admin_view.edges if e.broken} == {
        f"{world.hidden.key}-999",
        f"wiki:{world.hidden.key}/no-such-page",
    }
    await grant(db_session, world.reader, world.hidden, ["view_issues"])
    member_view = await links.get_links(db_session, world.reader, ("issue", probe_missing.id))
    assert [e.target for e in member_view.edges if e.broken] == [f"{world.hidden.key}-999"]


async def test_missing_targets_in_the_readers_own_project_are_shown(db_session, world):
    root = await page(db_session, world, world.home, "Plans", f"[[Not Yet]] and {world.home.key}-999")
    await rebuild(db_session, root)
    graph = await links.get_links(db_session, world.reader, ("wiki_page", root.id))
    assert {e.target for e in graph.edges if e.broken} == {f"wiki:{world.home.key}/not-yet", f"{world.home.key}-999"}


async def test_stats_do_not_tell_existing_hidden_issues_from_missing_ones(db_session, world, manager):
    existing = await issue(db_session, world, world.hidden, "Secret")
    first = await issue(db_session, world, world.home, "Mentions an existing one", f"See {existing.display_key}")
    await rebuild(db_session, first)
    with_existing = await project_stats(db_session, manager, world.home.id)

    first.description = f"See {world.hidden.key}-999"
    await db_session.commit()
    await rebuild(db_session, first)
    with_missing = await project_stats(db_session, manager, world.home.id)

    keys = ("total", "unresolved", "by_kind", "by_source", "top_unresolved")
    assert {k: with_existing[k] for k in keys} == {k: with_missing[k] for k in keys}
    assert with_missing["total"] == 0 and with_missing["top_unresolved"] == []

    admin_stats = await project_stats(db_session, world.admin, world.home.id)
    assert [u["target_ref"] for u in admin_stats["top_unresolved"]] == [f"{world.hidden.key}-999"]


async def test_a_project_limited_admin_key_gets_no_admin_reach_elsewhere(db_session, world):
    doc = await page(db_session, world, world.home, "Shared Doc")
    elsewhere = await issue(db_session, world, world.hidden, "Elsewhere")
    note = await comment(
        db_session,
        world,
        elsewhere,
        f"Secretly about [the doc](/projects/{world.home.key}/wiki/shared-doc/)",
        private=True,
    )
    file_on_note = Attachment(
        container_type="Journal",
        container_id=note.id,
        filename="minutes.pdf",
        disk_filename="eld-minutes-1",
        filesize=1,
        author_id=world.admin.id,
    )
    db_session.add(file_on_note)
    await db_session.commit()
    pointer = await issue(db_session, world, world.home, "Points at the file", f"[f](/attachments/{file_on_note.id}/)")
    own_note_issue = await issue(db_session, world, world.home, "Home issue")
    own_note = await comment(db_session, world, own_note_issue, "Internal: [[Shared Doc]]", private=True)
    await rebuild(db_session, note, pointer, own_note)

    unlimited = await links.get_links(db_session, world.admin, ("wiki_page", doc.id), direction="in")
    assert {e.via for e in unlimited.edges} == {
        f"{elsewhere.display_key}#{note.sequence}",
        f"{own_note_issue.display_key}#{own_note.sequence}",
    }

    with bound_scope(ProjectScope(frozenset({world.home.id}))):
        limited = await links.get_links(db_session, world.admin, ("wiki_page", doc.id), direction="in")
        # Its own project's private note still counts; the other project's does not.
        assert [e.via for e in limited.edges] == [f"{own_note_issue.display_key}#{own_note.sequence}"]
        files = await links.get_links(db_session, world.admin, ("issue", pointer.id))
        assert files.edges == [] and files.nodes == []
