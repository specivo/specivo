"""The entity link graph: storing the links written in text, resolving them, and reading them back.

Covers ``EntityLinkService``:

- rebuilding a source from its text (wiki pages, issue descriptions, comments),
  idempotently, with duplicates folded and self-links dropped;
- unresolved targets kept and resolved when the issue or page appears or is
  renamed, and links to a renamed page's old slug followed;
- what the database does on delete (CASCADE / SET NULL);
- the session events that queue a rebuild after a commit;
- ``get_links``: merging text links with relations, parent/child and
  attachments, and filtering everything for the reader.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.attachment import Attachment
from specivo.models.entity_link import EntityLink
from specivo.models.issue import Issue
from specivo.models.relation import IssueRelation
from specivo.services.entity_link_index import _after_soft_rollback, collect, defer_entity_links, queue_entity_links
from specivo.services.wiki_service import WikiService
from tests.integration.link_graph_helpers import (
    World,
    build_world,
    comment,
    issue,
    links,
    page,
    rebuild,
    wiki_ref,
)

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]


@pytest.fixture
async def world(db_session: AsyncSession) -> World:
    return await build_world(db_session, "ELG")


async def _rows(db: AsyncSession, **source) -> list[EntityLink]:
    ((column, value),) = source.items()
    stmt = select(EntityLink).where(getattr(EntityLink, column) == value).order_by(EntityLink.id)
    return list((await db.execute(stmt.execution_options(populate_existing=True))).scalars())


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


class TestRebuildSource:
    async def test_wiki_page_links_resolve_and_broken_ones_are_kept(self, db_session, world):
        target = await page(db_session, world, world.home, "Page B")
        source = await page(db_session, world, world.home, "Page A", "See [[Page B]] and [[Missing Page|later]].")
        await rebuild(db_session, source)

        rows = await _rows(db_session, source_wiki_page_id=source.id)
        assert [(r.kind, r.target_ref, r.target_wiki_page_id, r.display_text) for r in rows] == [
            ("wiki_link", "page-b", target.id, None),
            ("wiki_link", "missing-page", None, "later"),
        ]
        assert all(r.target_project_id == world.home.id and r.source_project_id == world.home.id for r in rows)

    async def test_rebuild_replaces_and_is_idempotent(self, db_session, world):
        source = await page(db_session, world, world.home, "Mutable", "[[Old Target]]")
        await rebuild(db_session, source)
        await WikiService().update_page(db_session, source.id, "[[New Target]] [[New Target]]", world.admin, 1)
        await db_session.commit()
        await rebuild(db_session, source)
        await rebuild(db_session, source)

        rows = await _rows(db_session, source_wiki_page_id=source.id)
        assert [r.target_ref for r in rows] == ["new-target"]

    async def test_issue_description_links_to_issues_wiki_and_instance_urls(self, db_session, world):
        other = await issue(db_session, world, world.hidden, "Elsewhere")
        doc = await page(db_session, world, world.home, "Design Notes")
        hidden_doc = await page(db_session, world, world.hidden, "Runbook")
        text = (
            f"Depends on {other.display_key}; UTF-8 and SHA-256 are not keys; {world.home.key}-999 is coming.\n\n"
            f"See [[Design Notes]] and the [runbook](/projects/{world.hidden.key}/wiki/runbook/).\n\n"
            f"`{world.home.key}-1` in code is not a link."
        )
        source = await issue(db_session, world, world.home, "Source", text)
        await rebuild(db_session, source)

        rows = await _rows(db_session, source_issue_id=source.id)
        got = {(r.kind, r.target_type, r.target_ref): (r.target_issue_id or r.target_wiki_page_id) for r in rows}
        assert got == {
            ("mention", "issue", other.display_key): other.id,
            ("mention", "issue", f"{world.home.key}-999"): None,
            ("wiki_link", "wiki_page", "design-notes"): doc.id,
            ("mention", "wiki_page", "runbook"): hidden_doc.id,
        }

    async def test_a_text_naming_its_own_issue_is_not_a_link(self, db_session, world):
        source = await issue(db_session, world, world.home, "Self")
        source.description = f"This is {source.display_key}."
        await db_session.commit()
        note = await comment(db_session, world, source, f"Still {source.display_key}.")
        await rebuild(db_session, source, note)
        assert await _rows(db_session, source_issue_id=source.id) == []
        assert await _rows(db_session, source_journal_id=note.id) == []

    async def test_comment_links(self, db_session, world):
        target = await issue(db_session, world, world.home, "Target")
        on = await issue(db_session, world, world.home, "Discussed")
        note = await comment(db_session, world, on, f"Duplicate of {target.display_key}, see [[FAQ]].")
        await rebuild(db_session, note)
        rows = await _rows(db_session, source_journal_id=note.id)
        assert {(r.target_type, r.target_ref) for r in rows} == {("issue", target.display_key), ("wiki_page", "faq")}

    async def test_a_source_that_is_gone_or_trashed_keeps_no_links(self, db_session, world):
        source = await page(db_session, world, world.home, "Trashed", "[[Somewhere]]")
        await rebuild(db_session, source)
        await WikiService().delete_page(db_session, source.id, world.admin)
        await db_session.commit()
        await rebuild(db_session, source)
        assert await _rows(db_session, source_wiki_page_id=source.id) == []

        assert await links.rebuild_source(db_session, "journal", 987654321) == 0

    async def test_the_cap_limits_links_per_source(self, db_session, world, monkeypatch):
        monkeypatch.setattr("specivo.services.entity_link_service.ENTITY_LINKS_MAX_PER_SOURCE", 3)
        source = await page(db_session, world, world.home, "Many", " ".join(f"[[P{i}]]" for i in range(10)))
        await rebuild(db_session, source)
        assert [r.target_ref for r in await _rows(db_session, source_wiki_page_id=source.id)] == ["p0", "p1", "p2"]

    async def test_attachment_filenames_resolve_and_wait_for_uploads(self, db_session, world):
        source = await issue(db_session, world, world.home, "With files", "![chart](chart.png) [notes](notes.txt)")
        chart = Attachment(
            container_type="Issue",
            container_id=source.id,
            filename="chart.png",
            disk_filename="elg-chart-1",
            filesize=1,
            author_id=world.admin.id,
        )
        db_session.add(chart)
        await db_session.commit()
        await rebuild(db_session, source)
        rows = {r.target_ref: r.target_attachment_id for r in await _rows(db_session, source_issue_id=source.id)}
        assert rows == {"chart.png": chart.id, "notes.txt": None}

        notes = Attachment(
            container_type="Issue",
            container_id=source.id,
            filename="notes.txt",
            disk_filename="elg-notes-1",
            filesize=1,
            author_id=world.admin.id,
        )
        db_session.add(notes)
        await db_session.commit()
        assert await links.expand_keys(db_session, [("attachment", notes.id)]) == [("issue", source.id)]


class TestResolution:
    async def test_creating_a_page_resolves_links_written_before_it(self, db_session, world):
        source = await page(db_session, world, world.home, "Early", "[[Later Page]]")
        await rebuild(db_session, source)
        later = await page(db_session, world, world.home, "Later Page")
        await rebuild(db_session, later)
        (row,) = await _rows(db_session, source_wiki_page_id=source.id)
        assert row.target_wiki_page_id == later.id

    async def test_renaming_resolves_links_to_the_new_slug_and_old_slug_links_follow(self, db_session, world):
        target = await page(db_session, world, world.home, "Draft Name")
        to_new = await page(db_session, world, world.home, "Links New", "[[Final Name]]")
        await rebuild(db_session, to_new)
        await WikiService().rename_page(db_session, target.id, "Final Name", target.lock_version)
        await db_session.commit()
        await rebuild(db_session, target)
        (row,) = await _rows(db_session, source_wiki_page_id=to_new.id)
        assert row.target_wiki_page_id == target.id

        to_old = await page(db_session, world, world.home, "Links Old", "[[Draft Name]]")
        await rebuild(db_session, to_old)
        (row,) = await _rows(db_session, source_wiki_page_id=to_old.id)
        assert row.target_wiki_page_id == target.id

    async def test_creating_an_issue_resolves_mentions_of_its_key(self, db_session, world):
        number = 1
        while (
            await db_session.execute(
                select(Issue.id).where(Issue.project_id == world.home.id, Issue.sequence_number == number)
            )
        ).first():
            number += 1
        source = await page(db_session, world, world.home, "Plan", f"Next: {world.home.key}-{number}")
        await rebuild(db_session, source)
        (row,) = await _rows(db_session, source_wiki_page_id=source.id)
        assert row.target_issue_id is None

        created = await issue(db_session, world, world.home, "Now it exists")
        assert created.sequence_number == number
        await rebuild(db_session, created)
        (row,) = await _rows(db_session, source_wiki_page_id=source.id)
        assert row.target_issue_id == created.id


class TestDatabaseCascades:
    async def test_deleting_a_source_removes_its_links_and_a_target_unresolves_them(self, db_session, world):
        target = await issue(db_session, world, world.home, "Doomed target")
        source = await issue(db_session, world, world.home, "Doomed source", f"See {target.display_key}")
        note = await comment(db_session, world, target, f"Back to {source.display_key}")
        await rebuild(db_session, source, note)

        await db_session.execute(delete(Issue).where(Issue.id == target.id))
        await db_session.commit()
        assert await _rows(db_session, source_journal_id=note.id) == []
        (row,) = await _rows(db_session, source_issue_id=source.id)
        assert row.target_issue_id is None and row.target_ref == target.display_key


# ---------------------------------------------------------------------------
# Session events
# ---------------------------------------------------------------------------


class TestSessionEvents:
    async def test_changes_are_queued_and_dispatched_once_after_commit(self, db_session, world, monkeypatch):
        dispatched: list[set] = []
        monkeypatch.setattr(
            "specivo.services.entity_link_index.dispatch_entity_links", lambda keys: dispatched.append(set(keys))
        )
        created = await issue(db_session, world, world.home, "Queued", "See [[FAQ]]")
        assert ("issue", created.id) in set().union(*dispatched)

        dispatched.clear()
        note = await comment(db_session, world, created, "A comment")
        doc = await page(db_session, world, world.home, "Queued Page")
        assert ("journal", note.id) in set().union(*dispatched)
        assert ("wiki_page", doc.id) in set().union(*dispatched)

    async def test_only_the_outermost_rollback_drops_the_queue(self, db_session):
        """A rolled-back savepoint keeps the queue (the task re-reads committed text); a real rollback empties it."""
        session = db_session.sync_session
        session.info["entity_links_pending"] = {("issue", 1)}
        _after_soft_rollback(session, SimpleNamespace(parent=object()))
        assert session.info["entity_links_pending"] == {("issue", 1)}
        _after_soft_rollback(session, SimpleNamespace(parent=None))
        assert "entity_links_pending" not in session.info

    async def test_only_text_changes_count(self, db_session, world):
        created = await issue(db_session, world, world.home, "Watched")
        db_session.sync_session.info.pop("entity_links_pending", None)
        created.subject = "Renamed only"
        await db_session.flush()
        assert collect(db_session.sync_session) == set()
        created.description = "Now with [[Text]]"
        assert collect(db_session.sync_session) == {("issue", created.id)}

    async def test_moving_an_issue_queues_its_comments(self, db_session, world):
        created = await issue(db_session, world, world.home, "Mover")
        created.project_id = world.hidden.id
        assert collect(db_session.sync_session) == {("issue", created.id), ("issue_comments", created.id)}

    async def test_deferred_sessions_queue_nothing(self, db_session, world):
        defer_entity_links(db_session)
        queue_entity_links(db_session, [("issue", 1)])
        await issue(db_session, world, world.home, "Imported")
        assert not db_session.sync_session.info.get("entity_links_pending")


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _edges(graph) -> set[tuple[str, str, str, str | None]]:
    return {(e.source, e.target, e.kind, e.direction) for e in graph.edges}


class TestGetLinks:
    async def test_merges_text_links_with_relations_and_hierarchy(self, db_session, world):
        doc = await page(db_session, world, world.home, "Spec")
        related = await issue(db_session, world, world.home, "Related")
        root = await issue(db_session, world, world.home, "Root", "Implements [[Spec]]")
        db_session.add(IssueRelation(issue_from_id=root.id, issue_to_id=related.id, relation_type="blocks"))
        mentioner = await page(db_session, world, world.home, "Status", f"Tracking {root.display_key}")
        await rebuild(db_session, root, mentioner)

        graph = await links.get_links(db_session, world.reader, ("issue", root.id))
        spec, status = wiki_ref(world.home, "spec"), wiki_ref(world.home, "status")
        assert _edges(graph) == {
            (root.display_key, spec, "wiki_link", "out"),
            (status, root.display_key, "mention", "in"),
            (root.display_key, related.display_key, "relation", "out"),
        }
        assert next(e for e in graph.edges if e.kind == "relation").relation_type == "blocks"
        assert {n.ref: n.type for n in graph.nodes} == {
            spec: "wiki_page",
            status: "wiki_page",
            related.display_key: "issue",
        }
        assert graph.root is not None and graph.root.ref == root.display_key

        backlinks = await links.get_links(db_session, world.reader, ("issue", root.id), direction="in")
        assert _edges(backlinks) == {(status, root.display_key, "mention", "in")}
        only = await links.get_links(db_session, world.reader, ("issue", root.id), kinds=["relation"])
        assert {e.kind for e in only.edges} == {"relation"}

    async def test_comment_links_are_attributed_to_their_issue(self, db_session, world):
        doc = await page(db_session, world, world.home, "Handbook")
        on = await issue(db_session, world, world.home, "Asked")
        note = await comment(db_session, world, on, "Answered in [[Handbook]]")
        await rebuild(db_session, note)
        graph = await links.get_links(db_session, world.reader, ("wiki_page", doc.id), direction="in")
        (edge,) = graph.edges
        assert (edge.source, edge.kind, edge.via) == (on.display_key, "wiki_link", f"{on.display_key}#{note.sequence}")

    async def test_broken_links_are_listed_outgoing(self, db_session, world):
        root = await page(db_session, world, world.home, "Hub", "[[Nowhere]]")
        await rebuild(db_session, root)
        graph = await links.get_links(db_session, world.reader, ("wiki_page", root.id))
        (edge,) = graph.edges
        assert edge.broken and edge.target == wiki_ref(world.home, "nowhere")

    async def test_depth_two_and_the_limit(self, db_session, world):
        far = await page(db_session, world, world.home, "Far")
        near = await page(db_session, world, world.home, "Near", "[[Far]]")
        root = await page(db_session, world, world.home, "Start", "[[Near]] [[Other]]")
        other = await page(db_session, world, world.home, "Other")
        await rebuild(db_session, near, root)

        one = await links.get_links(db_session, world.reader, ("wiki_page", root.id), direction="out")
        assert {n.ref for n in one.nodes} == {wiki_ref(world.home, "near"), wiki_ref(world.home, "other")}
        two = await links.get_links(db_session, world.reader, ("wiki_page", root.id), direction="out", depth=2)
        assert wiki_ref(world.home, "far") in {n.ref for n in two.nodes}
        assert (wiki_ref(world.home, "near"), wiki_ref(world.home, "far"), "wiki_link", None) in _edges(two)
        capped = await links.get_links(db_session, world.reader, ("wiki_page", root.id), direction="out", limit=1)
        assert len(capped.nodes) == 1 and capped.truncated
        assert far.id and other.id

    async def test_bad_arguments(self, db_session, world):
        root = await page(db_session, world, world.home, "Args")
        for kwargs in ({"direction": "sideways"}, {"depth": 3}, {"kinds": ["nonsense"]}):
            with pytest.raises(ValueError):
                await links.get_links(db_session, world.reader, ("wiki_page", root.id), **kwargs)


class TestPermissions:
    """A neighbour the reader may not see is left out with its edge; its title never shows."""

    async def test_hidden_project_issue_and_wiki_are_omitted(self, db_session, world):
        secret_issue = await issue(db_session, world, world.hidden, "Secret issue")
        secret_page = await page(db_session, world, world.hidden, "Secret page", "")
        root = await issue(
            db_session,
            world,
            world.home,
            "Visible",
            f"{secret_issue.display_key} and [x](/projects/{world.hidden.key}/wiki/secret-page/)",
        )
        back = await page(db_session, world, world.hidden, "Secret notes", f"About {root.display_key}")
        await rebuild(db_session, root, back)

        reader_graph = await links.get_links(db_session, world.reader, ("issue", root.id))
        assert reader_graph.edges == [] and reader_graph.nodes == []
        admin_graph = await links.get_links(db_session, world.admin, ("issue", root.id))
        assert {n.title for n in admin_graph.nodes} == {"Secret issue", "Secret page", "Secret notes"}
        assert secret_page.id

    async def test_private_issue_is_omitted(self, db_session, world):
        private = await issue(db_session, world, world.home, "Private one", private=True)
        root = await page(db_session, world, world.home, "Index", f"See {private.display_key}")
        await rebuild(db_session, root)
        graph = await links.get_links(db_session, world.reader, ("wiki_page", root.id))
        assert graph.edges == []

    async def test_wiki_without_view_wiki_or_module_or_in_trash_is_omitted(self, db_session, world):
        from specivo.models.project import EnabledModule

        doc = await page(db_session, world, world.home, "Guide")
        root = await issue(db_session, world, world.home, "Needs guide", "[[Guide]]")
        await rebuild(db_session, root)
        assert len((await links.get_links(db_session, world.reader, ("issue", root.id))).edges) == 1

        await db_session.execute(
            delete(EnabledModule).where(EnabledModule.project_id == world.home.id, EnabledModule.name == "wiki")
        )
        await db_session.commit()
        assert (await links.get_links(db_session, world.reader, ("issue", root.id))).edges == []
        db_session.add(EnabledModule(project_id=world.home.id, name="wiki"))
        await db_session.commit()

        await WikiService().delete_page(db_session, doc.id, world.admin)
        await db_session.commit()
        assert (await links.get_links(db_session, world.admin, ("issue", root.id))).edges == []

    async def test_private_notes_count_for_admins_only(self, db_session, world):
        doc = await page(db_session, world, world.home, "Internal")
        on = await issue(db_session, world, world.home, "Has a private note")
        note = await comment(db_session, world, on, "See [[Internal]]", private=True)
        await rebuild(db_session, note)
        assert (await links.get_links(db_session, world.reader, ("wiki_page", doc.id))).edges == []
        assert len((await links.get_links(db_session, world.admin, ("wiki_page", doc.id))).edges) == 1


# ---------------------------------------------------------------------------
# Wiki graph
# ---------------------------------------------------------------------------


async def test_wiki_graph_lists_live_pages_and_flags_broken_links(db_session, world):
    alpha = await page(db_session, world, world.home, "Alpha", "[[Beta]] [[Gamma]] [[Nope]]")
    beta = await page(db_session, world, world.home, "Beta", "[[Alpha]]")
    gamma = await page(db_session, world, world.home, "Gamma")
    await rebuild(db_session, alpha, beta)
    await WikiService().delete_page(db_session, gamma.id, world.admin)
    await db_session.commit()

    wiki = await WikiService().get_wiki(db_session, world.home.id)
    assert wiki is not None
    graph = await links.get_wiki_graph(db_session, wiki.id)
    assert {n["slug"] for n in graph["nodes"]} == {"alpha", "beta"}
    edges = {(e["source_page_id"], e["target_slug"]): e["is_broken"] for e in graph["edges"]}
    assert edges == {
        (alpha.id, "beta"): False,
        (alpha.id, "gamma"): True,
        (alpha.id, "nope"): True,
        (beta.id, "alpha"): False,
    }
