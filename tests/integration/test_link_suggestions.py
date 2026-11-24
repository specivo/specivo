"""Suggested links: document vectors, the neighbours stored as suggestions, how the link graph shows
them, and accepting or dismissing one (service level).

- A document vector is the L2-normalised mean of an item's chunk embeddings.
- Suggestions pair items of one project, with one model, at or above the
  project's threshold, at most top-k per item; pairs linked in any way and
  pairs already decided are never suggested.
- The link graph returns them (kind ``similar``) only when asked, and only
  when the reader may see both ends.
- Accepting makes a relation (two issues) or a typed link; dismissing keeps
  the pair out for good. Both need the permission that makes the link.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import NotFoundError, PermissionDeniedError, ValidationError
from specivo.models.entity_link import KIND_TYPED_LINK, EntityLink
from specivo.models.link_suggestion import EntitySuggestion, EntityVector
from specivo.models.relation import IssueRelation
from specivo.models.search import SearchSource
from specivo.services import link_suggestion_service as svc
from specivo.services.entity_link_service import KIND_SIMILAR, EntityLinkService
from tests.integration.link_graph_helpers import World, build_world, comment, grant, issue, page, rebuild, wiki_ref
from tests.integration.link_suggestion_helpers import (
    _no_queued_tasks,
    add_permission,
    cos,
    default_model,
    embed,
    key,
    unit,
)

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

assert _no_queued_tasks  # the autouse fixture, imported to be active here

A = [1.0, 0.0, 0.0, 0.0]
NEAR_A = [0.95, 0.31, 0.0, 0.0]  # cos 0.95 with A
ALSO_NEAR_A = [0.9, 0.0, 0.43, 0.0]  # cos 0.90 with A
FAR = [0.0, 0.0, 0.0, 1.0]

links = EntityLinkService()


@pytest.fixture
async def world(db_session: AsyncSession) -> World:
    return await build_world(db_session, "SGS")


@pytest.fixture
async def model(db_session: AsyncSession):
    return await default_model(db_session, "sgs-mock")


async def _suggested(db: AsyncSession) -> set[frozenset]:
    rows = (await db.execute(select(EntitySuggestion).where(EntitySuggestion.status == "suggested"))).scalars()
    return {frozenset({row.source, row.target}) for row in rows}


def _pair(a, b) -> frozenset:
    return frozenset({key(a), key(b)})


# -- vectors ------------------------------------------------------------------


async def test_vector_is_the_normalised_mean_of_the_chunks(db_session, world, model):
    item = await issue(db_session, world, world.home, "Two chunks")
    await embed(db_session, model, item, [2.0, 0.0, 0.0, 0.0], [0.0, 2.0, 0.0, 0.0])

    changed = await svc.refresh_vectors(db_session, model.id, items=[key(item)])
    assert changed == {key(item)}
    vector = await db_session.scalar(
        select(EntityVector.vector)
        .join(SearchSource, SearchSource.id == EntityVector.search_source_id)
        .where(SearchSource.source_type == "issue", SearchSource.entity_id == item.id)
    )
    assert [round(float(v), 4) for v in vector] == [0.7071, 0.7071, 0.0, 0.0]

    # Nothing changed: nothing is reported, so no computation is queued.
    assert await svc.refresh_vectors(db_session, model.id, items=[key(item)]) == set()


async def test_vector_goes_when_the_embeddings_go(db_session, world, model):
    from specivo.models.search import ChunkEmbedding

    item = await issue(db_session, world, world.home, "Emptied")
    await embed(db_session, model, item, A)
    await svc.refresh_vectors(db_session, model.id, project_id=world.home.id)
    await db_session.execute(ChunkEmbedding.__table__.delete().where(ChunkEmbedding.model_id == model.id))
    assert await svc.refresh_vectors(db_session, model.id, items=[key(item)]) == {key(item)}
    assert await db_session.scalar(select(func.count()).select_from(EntityVector)) == 0


# -- computing ------------------------------------------------------------------


async def test_project_suggestions_keep_close_pairs_of_one_project(db_session, world, model):
    root = await issue(db_session, world, world.home, "Root")
    near = await page(db_session, world, world.home, "Near page")
    also = await issue(db_session, world, world.home, "Also near")
    far = await page(db_session, world, world.home, "Far page")
    elsewhere = await issue(db_session, world, world.hidden, "Same vector, other project")
    for item, vector in ((root, A), (near, NEAR_A), (also, ALSO_NEAR_A), (far, FAR), (elsewhere, A)):
        await embed(db_session, model, item, vector)

    stored = await svc.compute_for_project(db_session, world.home.id)
    pairs = await _suggested(db_session)
    assert pairs == {_pair(root, near), _pair(root, also), _pair(near, also)}
    assert stored == 3
    assert cos(NEAR_A, ALSO_NEAR_A) >= 0.80
    rows = (await db_session.execute(select(EntitySuggestion))).scalars().all()
    assert {row.project_id for row in rows} == {world.home.id}
    by_pair = {frozenset({r.source, r.target}): r.score for r in rows}
    assert by_pair[_pair(root, near)] == pytest.approx(cos(A, NEAR_A), abs=1e-4)


async def test_threshold_is_per_project_and_top_k_caps_each_item(db_session, world, model, monkeypatch):
    root = await issue(db_session, world, world.home, "Root")
    near = await page(db_session, world, world.home, "Near")
    also = await issue(db_session, world, world.home, "Also")
    for item, vector in ((root, A), (near, NEAR_A), (also, ALSO_NEAR_A)):
        await embed(db_session, model, item, vector)

    world.home.settings = {**(world.home.settings or {}), svc.THRESHOLD_SETTING: 0.93}
    await db_session.commit()
    await svc.compute_for_project(db_session, world.home.id)
    assert await _suggested(db_session) == {_pair(root, near)}

    world.home.settings = {**world.home.settings, svc.THRESHOLD_SETTING: 0.5}
    await db_session.commit()
    monkeypatch.setattr(svc, "SIMILAR_LINKS_TOP_K", 1)
    await svc.compute_for_project(db_session, world.home.id)
    # Each item keeps its single best neighbour: root-near (root, near), near-root, also-root.
    assert await _suggested(db_session) == {_pair(root, near), _pair(also, root)}


async def test_linked_pairs_are_never_suggested(db_session, world, model):
    root = await issue(db_session, world, world.home, "Root", "See [[Documented]]")
    documented = await page(db_session, world, world.home, "Documented")
    related = await issue(db_session, world, world.home, "Related")
    child = await issue(db_session, world, world.home, "Child")
    commented = await issue(db_session, world, world.home, "Commented")
    child.parent_id = root.id
    db_session.add(IssueRelation(issue_from_id=root.id, issue_to_id=related.id, relation_type="relates"))
    await db_session.commit()
    journal = await comment(db_session, world, commented, f"Same as {root.display_key}")
    await rebuild(db_session, root, journal)
    for item in (root, documented, related, child, commented):
        await embed(db_session, model, item, A)

    await svc.compute_for_project(db_session, world.home.id)
    pairs = await _suggested(db_session)
    for other in (documented, related, child, commented):
        assert _pair(root, other) not in pairs
    # Items not linked to each other are still suggested.
    assert _pair(documented, related) in pairs


async def test_dismissed_pairs_stay_out_after_any_recomputation(db_session, world, model):
    root = await issue(db_session, world, world.home, "Root")
    near = await page(db_session, world, world.home, "Near")
    for item, vector in ((root, A), (near, NEAR_A)):
        await embed(db_session, model, item, vector)
    await svc.compute_for_project(db_session, world.home.id)

    decision = await svc.decide(db_session, world.admin, "dismiss", key(root), key(near))
    assert decision.status == "dismissed"
    await db_session.commit()

    await svc.compute_for_project(db_session, world.home.id)
    await svc.compute_for_items(db_session, [key(root), key(near)])
    assert await _suggested(db_session) == set()
    row = (await db_session.execute(select(EntitySuggestion))).scalar_one()
    assert (row.status, row.decided_by_id) == ("dismissed", world.admin.id)


async def test_items_recomputation_replaces_only_their_suggestions(db_session, world, model):
    root = await issue(db_session, world, world.home, "Root")
    near = await page(db_session, world, world.home, "Near")
    other = await issue(db_session, world, world.home, "Other")
    other_near = await page(db_session, world, world.home, "Other near")
    for item, vector in ((root, A), (near, NEAR_A), (other, FAR), (other_near, unit(0.1, 0, 0, 1))):
        await embed(db_session, model, item, vector)
    await svc.compute_for_project(db_session, world.home.id)
    assert await _suggested(db_session) == {_pair(root, near), _pair(other, other_near)}

    # The root is emptied: its vector goes, and with it its suggestions; the others stay.
    from specivo.models.search import ChunkEmbedding, SearchChunk

    root_chunks = (
        select(SearchChunk.id)
        .join(SearchSource, SearchSource.id == SearchChunk.source_id)
        .where(SearchSource.source_type == "issue", SearchSource.entity_id == root.id)
    )
    await db_session.execute(ChunkEmbedding.__table__.delete().where(ChunkEmbedding.chunk_id.in_(root_chunks)))
    changed = await svc.refresh_vectors(db_session, model.id, items=[key(root)])
    await svc.compute_for_items(db_session, changed)
    assert await _suggested(db_session) == {_pair(other, other_near)}


async def test_no_default_model_computes_nothing(db_session, world):
    await issue(db_session, world, world.home, "No model")
    assert await svc.compute_for_project(db_session, world.home.id) == 0
    assert await svc.compute_for_items(db_session, [("issue", 1)]) == 0


# -- reading -------------------------------------------------------------------


async def _graph_kinds(db, user, root, **kwargs):
    graph = await links.get_links(db, user, key(root), **kwargs)
    return graph


async def test_similar_edges_come_only_when_asked_and_carry_score(db_session, world, model):
    root = await issue(db_session, world, world.home, "Root")
    near = await page(db_session, world, world.home, "Near")
    for item, vector in ((root, A), (near, NEAR_A)):
        await embed(db_session, model, item, vector)
    await svc.compute_for_project(db_session, world.home.id)

    default = await links.get_links(db_session, world.reader, key(root))
    assert all(e.kind != KIND_SIMILAR for e in default.edges)
    assert default.nodes == []

    graph = await links.get_links(db_session, world.reader, key(root), kinds=[KIND_SIMILAR])
    (edge,) = graph.as_dict()["edges"]
    assert edge == {
        "source": root.display_key,
        "target": wiki_ref(world.home, "near"),
        "kind": "similar",
        "direction": "out",
        "label": f"{cos(A, NEAR_A):.3f}",
        "suggested": True,
    }
    # From the page, the same suggestion points the other way.
    back = await links.get_links(db_session, world.reader, key(near), kinds=[KIND_SIMILAR], direction="in")
    assert [(e.source, e.target) for e in back.edges] == [(wiki_ref(world.home, "near"), root.display_key)]


async def test_a_suggestion_needs_both_ends_visible(db_session, world, model):
    root = await issue(db_session, world, world.home, "Root")
    private = await issue(db_session, world, world.home, "Private twin", private=True)
    page_twin = await page(db_session, world, world.home, "Page twin")
    for item in (root, private, page_twin):
        await embed(db_session, model, item, A)
    await svc.compute_for_project(db_session, world.home.id)
    assert _pair(root, private) in await _suggested(db_session)

    graph = await links.get_links(db_session, world.reader, key(root), kinds=[KIND_SIMILAR])
    assert {n.ref for n in graph.nodes} == {wiki_ref(world.home, "page-twin")}
    assert "Private twin" not in str(graph.as_dict())
    admin_graph = await links.get_links(db_session, world.admin, key(root), kinds=[KIND_SIMILAR])
    assert private.display_key in {n.ref for n in admin_graph.nodes}


async def test_a_pair_linked_after_the_computation_is_no_longer_shown(db_session, world, model):
    root = await issue(db_session, world, world.home, "Root")
    other = await issue(db_session, world, world.home, "Other")
    for item in (root, other):
        await embed(db_session, model, item, A)
    await svc.compute_for_project(db_session, world.home.id)
    db_session.add(IssueRelation(issue_from_id=root.id, issue_to_id=other.id, relation_type="relates"))
    await db_session.commit()
    graph = await links.get_links(db_session, world.admin, key(root), kinds=[KIND_SIMILAR])
    assert graph.edges == []


# -- deciding -------------------------------------------------------------------


async def test_accepting_two_issues_makes_a_relation(db_session, world, model):
    root = await issue(db_session, world, world.home, "Root")
    other = await issue(db_session, world, world.home, "Other")
    for item in (root, other):
        await embed(db_session, model, item, A)
    await svc.compute_for_project(db_session, world.home.id)

    decision = await svc.decide(db_session, world.admin, "accept", key(root), key(other), link_type="blocks")
    await db_session.commit()
    assert (decision.status, decision.link_kind, decision.link_type) == ("accepted", "relation", "blocks")
    relation = (await db_session.execute(select(IssueRelation))).scalar_one()
    assert (relation.issue_from_id, relation.issue_to_id, relation.relation_type) == (root.id, other.id, "blocks")
    assert "relation" in decision.describe()

    with pytest.raises(ValidationError):
        await svc.decide(db_session, world.admin, "accept", key(root), key(other))


async def test_accepting_an_issue_and_a_page_makes_a_typed_link_that_survives_rebuilds(db_session, world, model):
    root = await issue(db_session, world, world.home, "Root", "Plain text, no links")
    spec = await page(db_session, world, world.home, "Spec")
    for item in (root, spec):
        await embed(db_session, model, item, A)
    await svc.compute_for_project(db_session, world.home.id)

    decision = await svc.decide(db_session, world.admin, "accept", key(spec), key(root), link_type="documents")
    await db_session.commit()
    assert (decision.link_kind, decision.link_type) == (KIND_TYPED_LINK, "documents")
    link = (await db_session.execute(select(EntityLink))).scalar_one()
    assert (link.kind, link.link_type, link.source_wiki_page_id, link.target_issue_id) == (
        KIND_TYPED_LINK,
        "documents",
        spec.id,
        root.id,
    )
    assert link.target_ref == root.display_key

    # Re-parsing the page's text keeps the typed link: it is not written in the text.
    await rebuild(db_session, spec, root)
    graph = await links.get_links(db_session, world.reader, key(root))
    assert [(e.source, e.kind, e.label, e.direction) for e in graph.edges] == [
        (wiki_ref(world.home, "spec"), KIND_TYPED_LINK, "documents", "in")
    ]
    # And the pair is linked now: never suggested again.
    await svc.compute_for_project(db_session, world.home.id)
    assert await _suggested(db_session) == set()


async def test_link_type_must_fit_the_pair(db_session, world, model):
    root = await issue(db_session, world, world.home, "Root")
    other = await issue(db_session, world, world.home, "Other")
    spec = await page(db_session, world, world.home, "Spec")
    for item in (root, other, spec):
        await embed(db_session, model, item, A)
    await svc.compute_for_project(db_session, world.home.id)
    with pytest.raises(ValidationError):
        await svc.decide(db_session, world.admin, "accept", key(root), key(other), link_type="documents")
    with pytest.raises(ValidationError):
        await svc.decide(db_session, world.admin, "accept", key(root), key(spec), link_type="blocks")
    with pytest.raises(ValidationError):
        await svc.decide(db_session, world.admin, "bogus", key(root), key(spec))
    with pytest.raises(ValidationError):
        await svc.decide(db_session, world.admin, "accept", key(root), key(root))


async def test_deciding_needs_the_permission_that_makes_the_link(db_session, world, model):
    root = await issue(db_session, world, world.home, "Root")
    other = await issue(db_session, world, world.home, "Other")
    spec = await page(db_session, world, world.home, "Spec")
    for item in (root, other, spec):
        await embed(db_session, model, item, A)
    await svc.compute_for_project(db_session, world.home.id)

    # The reader may only view issues and the wiki.
    for source, target in ((root, other), (root, spec), (spec, root)):
        for op in ("accept", "dismiss"):
            with pytest.raises(PermissionDeniedError):
                await svc.decide(db_session, world.reader, op, key(source), key(target))
    assert len(await _suggested(db_session)) == 3

    await add_permission(db_session, world.reader, world.home, "manage_issue_relations")
    decision = await svc.decide(db_session, world.reader, "dismiss", key(root), key(other))
    assert decision.status == "dismissed"
    with pytest.raises(PermissionDeniedError):
        await svc.decide(db_session, world.reader, "accept", key(root), key(spec))


async def test_deciding_a_pair_with_no_suggestion_is_not_found(db_session, world, model):
    root = await issue(db_session, world, world.home, "Root")
    far = await page(db_session, world, world.home, "Far")
    await embed(db_session, model, root, A)
    await embed(db_session, model, far, FAR)
    await svc.compute_for_project(db_session, world.home.id)
    with pytest.raises(NotFoundError):
        await svc.decide(db_session, world.admin, "accept", key(root), key(far))


async def test_typed_links_count_in_paths_and_inline_lines(db_session, world, model):
    from specivo.mcp.tools.links import _link_lines
    from specivo.services.entity_link_query import find_path

    root = await issue(db_session, world, world.home, "Root")
    spec = await page(db_session, world, world.home, "Spec")
    for item in (root, spec):
        await embed(db_session, model, item, A)
    await svc.compute_for_project(db_session, world.home.id)
    await svc.decide(db_session, world.admin, "accept", key(root), key(spec), link_type="implements")
    await db_session.commit()

    path = await find_path(db_session, world.reader, key(root), key(spec))
    assert path.edges is not None and [e.kind for e in path.edges] == [KIND_TYPED_LINK]
    lines = await _link_lines(db_session, world.reader, key(root))
    assert f"Links to: {wiki_ref(world.home, 'spec')} Spec (implements)" in lines


async def test_deciding_checks_visibility_itself(db_session, world, model):
    root = await issue(db_session, world, world.home, "Root")
    private = await issue(db_session, world, world.home, "Private", private=True)
    for item in (root, private):
        await embed(db_session, model, item, A)
    await svc.compute_for_project(db_session, world.home.id)
    await add_permission(db_session, world.reader, world.home, "manage_issue_relations")
    with pytest.raises(NotFoundError):
        await svc.decide(db_session, world.reader, "dismiss", key(root), key(private))


async def test_a_pair_across_projects_needs_the_permission_in_both(db_session, world, model):
    root = await issue(db_session, world, world.home, "Root")
    spec = await page(db_session, world, world.home, "Spec")
    for item in (root, spec):
        await embed(db_session, model, item, A)
    await svc.compute_for_project(db_session, world.home.id)
    # The issue moves to a project where the reader may edit issues; the page stays where they may only read.
    root.project_id = world.hidden.id
    await db_session.commit()
    await grant(db_session, world.reader, world.hidden, ["view_issues", "edit_issues"])
    with pytest.raises(PermissionDeniedError):
        await svc.decide(db_session, world.reader, "dismiss", key(root), key(spec))
    await add_permission(db_session, world.reader, world.home, "manage_wiki")
    decision = await svc.decide(db_session, world.reader, "dismiss", key(root), key(spec))
    assert decision.status == "dismissed"
