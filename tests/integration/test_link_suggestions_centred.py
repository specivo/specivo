"""Suggested links are gated on the centred score, and a project needs enough items to get any.

Every document vector of a project shares a large common direction, so two
unrelated items already have a high raw cosine. A pair is suggested only when
its cosine around the project's mean vector reaches the project's threshold;
the stored score stays the raw cosine. A project with fewer than
``SIMILAR_LINKS_MIN_ITEMS`` vectors gets no suggestions at all.
"""

from __future__ import annotations

import itertools

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.constants import SIMILAR_LINKS_MIN_ITEMS
from specivo.models.link_suggestion import EntitySuggestion
from specivo.services import link_suggestion_service as svc
from tests.integration.link_graph_helpers import World, build_world, issue, page
from tests.integration.link_suggestion_helpers import (
    DIM,
    _no_queued_tasks,
    cos,
    default_model,
    embed,
    key,
)

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

assert _no_queued_tasks  # the autouse fixture, imported to be active here

CLUSTER = 12
SHARED_DIM = DIM - 1  # the direction only the closer pair shares


def _member(n: int, *, shares: bool = False) -> list[float]:
    """The common direction, a little of its own, and for the closer pair a direction they share."""
    vector = [0.0] * DIM
    vector[0] = 1.0
    vector[1 + n] = 0.35
    if shares:
        vector[SHARED_DIM] = 0.5
    return vector


@pytest.fixture
async def world(db_session: AsyncSession) -> World:
    return await build_world(db_session, "SGC")


@pytest.fixture
async def model(db_session: AsyncSession):
    return await default_model(db_session, "sgc-mock")


@pytest.fixture
async def cluster(db_session: AsyncSession, world: World, model):
    """Eight issues and four pages, all alike; issue 0 and page 0 share one more direction."""
    items = [await issue(db_session, world, world.home, f"Cluster issue {n}") for n in range(8)]
    items += [await page(db_session, world, world.home, f"Cluster page {n}") for n in range(4)]
    vectors = [_member(n, shares=n in (0, 8)) for n in range(CLUSTER)]
    for item, vector in zip(items, vectors, strict=True):
        await embed(db_session, model, item, vector)
    return items, vectors


async def _suggested(db: AsyncSession) -> dict[frozenset, float]:
    rows = (await db.execute(select(EntitySuggestion).where(EntitySuggestion.status == "suggested"))).scalars()
    return {frozenset({row.source, row.target}): row.score for row in rows}


async def test_only_the_pair_closer_than_typical_is_suggested(db_session, world, cluster):
    items, vectors = cluster
    closer = frozenset({key(items[0]), key(items[8])})
    # Every pair is close in raw terms: the old raw threshold (0.80) would have let all 66 through.
    raw = [cos(a, b) for a, b in itertools.combinations(vectors, 2)]
    assert min(raw) > 0.80
    assert max(cos(vectors[a], vectors[b]) for a, b in itertools.combinations(range(1, 8), 2)) > 0.88

    stored = await svc.compute_for_project(db_session, world.home.id)
    suggested = await _suggested(db_session)
    assert set(suggested) == {closer}
    assert stored == 1
    # The stored (and displayed) score is the raw cosine, not the centred one.
    assert suggested[closer] == pytest.approx(cos(vectors[0], vectors[8]), abs=1e-4)


async def test_the_items_recomputation_gates_the_same_way(db_session, world, model, cluster):
    items, _vectors = cluster
    await svc.refresh_vectors(db_session, model.id, project_id=world.home.id)
    await svc.compute_for_items(db_session, [key(items[0]), key(items[3])])
    assert set(await _suggested(db_session)) == {frozenset({key(items[0]), key(items[8])})}


async def test_the_project_threshold_applies_to_the_centred_score(db_session, world, cluster):
    items, _vectors = cluster
    closer = frozenset({key(items[0]), key(items[8])})

    # The closer pair is about 0.59 centred, the others below 0: 0.95 drops it, 0.0 keeps only it.
    world.home.settings = {**(world.home.settings or {}), svc.THRESHOLD_SETTING: 0.95}
    await db_session.commit()
    assert await svc.compute_for_project(db_session, world.home.id) == 0

    world.home.settings = {**world.home.settings, svc.THRESHOLD_SETTING: 0.0}
    await db_session.commit()
    await svc.compute_for_project(db_session, world.home.id)
    assert set(await _suggested(db_session)) == {closer}


async def test_a_project_with_too_few_vectors_gets_no_suggestions(db_session, world, model):
    members = [await issue(db_session, world, world.home, f"Member {n}") for n in range(SIMILAR_LINKS_MIN_ITEMS - 1)]
    for n, member in enumerate(members):
        await embed(db_session, model, member, _member(n, shares=n < 2))
    pair = frozenset({key(members[0]), key(members[1])})
    # The first two would pass the threshold, but the project is one item short of the minimum.
    assert await svc.compute_for_project(db_session, world.home.id) == 0
    assert await svc.compute_for_items(db_session, [key(m) for m in members]) == 0
    assert await _suggested(db_session) == {}

    # Items of another project do not count towards this one's minimum.
    for n in range(3):
        elsewhere = await issue(db_session, world, world.hidden, f"Elsewhere {n}")
        await embed(db_session, model, elsewhere, _member(n))
    assert await svc.compute_for_project(db_session, world.home.id) == 0

    # One more item reaches the minimum: the pair is suggested now.
    last = await issue(db_session, world, world.home, "One more")
    await embed(db_session, model, last, _member(SIMILAR_LINKS_MIN_ITEMS))
    assert await svc.compute_for_project(db_session, world.home.id) == 1
    assert set(await _suggested(db_session)) == {pair}
