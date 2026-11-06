"""Semantic and hybrid search drop hits below the minimum similarity.

Without a cut-off, semantic search returns the nearest neighbours of any query,
so an unrelated query "matches" the whole index. These tests embed chunks as
fixed vectors at a known cosine similarity to a fixed query vector and check:

- an unrelated query finds nothing, in the results and in the total;
- a related query finds only the chunks above the cut-off;
- hybrid search keeps keyword hits but drops semantic-only noise, and its
  total agrees with the results;
- the ``search_semantic_min_similarity`` setting replaces the model default;
- the vector-index path stops widening once it reaches chunks below the cut-off;
- the search API returns the normal empty response.

The model is named ``multilingual-e5-small`` (the bundled model, default
cut-off 0.82) but uses the mock provider; query embeddings are patched in.
"""

from __future__ import annotations

import logging
import math
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.core.constants import SEMANTIC_INDEX_DIMENSIONS
from specivo.models.project import Project
from specivo.models.search import EmbeddingModel
from specivo.models.user import User
from specivo.services import search_service as search_module
from specivo.services.embedding_service import EmbeddingService
from specivo.services.search_service import SearchService
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import TEST_PASSWORD, AdminUserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

DIMS = SEMANTIC_INDEX_DIMENSIONS
E5_DEFAULT = 0.82
RELATED = 0.88  # a paraphrase of the chunk, measured range 0.81-0.89
NOISE = 0.76  # an unrelated chunk, measured median 0.74, ceiling 0.81


def _at_similarity(similarity: float, axis: int) -> list[float]:
    """A unit vector whose cosine similarity to the query (axis 0) is *similarity*.

    *axis* (1 or more) picks the orthogonal direction, so distinct chunks get
    distinct vectors.
    """
    vec = [0.0] * DIMS
    vec[0] = similarity
    vec[axis] = math.sqrt(1.0 - similarity * similarity)
    return vec


def _literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{v:.6f}" for v in vec) + "]"


@dataclass
class World:
    model: EmbeddingModel
    admin: User
    project: Project
    lookups: dict[str, int]


@pytest_asyncio.fixture
async def world(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[World]:
    status = StatusFactory.build(name="Sim New")
    db_session.add(status)
    await db_session.flush()
    tracker = TrackerFactory.build(name="Sim Bug", default_status_id=status.id)
    priority = PriorityFactory.build(name="Sim Normal")
    admin = AdminUserFactory.build(login="minsim_admin", status="active")
    project = ProjectFactory.build(key="MINSIM", identifier="min-similarity", is_public=False)
    db_session.add_all([tracker, priority, admin, project])
    await db_session.flush()

    await db_session.execute(update(EmbeddingModel).where(EmbeddingModel.is_default.is_(True)).values(is_default=False))
    model = EmbeddingModel(
        name="minsim-e5", provider="mock", model_name="multilingual-e5-small", dimensions=DIMS, is_default=True
    )
    db_session.add(model)
    await db_session.commit()

    query_vector = [0.0] * DIMS
    query_vector[0] = 1.0

    async def fixed_embedding(self, text_: str, model_: EmbeddingModel, intent: str = "passage") -> list[float]:
        return query_vector

    monkeypatch.setattr(EmbeddingService, "generate_embedding", fixed_embedding)
    monkeypatch.setattr(get_settings(), "search_semantic_min_similarity", None)
    yield World(
        model=model,
        admin=admin,
        project=project,
        lookups={"tracker": tracker.id, "status": status.id, "priority": priority.id, "author": admin.id},
    )


_seq: dict[int, int] = {}


async def _add_issues(db: AsyncSession, world: World, similarities: list[float], subject: str) -> list[int]:
    """Insert one issue per similarity, with one chunk embedded at that similarity."""
    start = _seq.get(world.project.id, 0)
    _seq[world.project.id] = start + len(similarities)
    ids: list[int] = []
    for n, similarity in enumerate(similarities, start=start + 1):
        issue_id = (
            await db.execute(
                text(
                    "INSERT INTO issues (project_id, project_key, sequence_number, tracker_id, status_id, "
                    "priority_id, author_id, subject, lock_version) "
                    "VALUES (:p, :k, :n, :t, :s, :pr, :a, :subj, 1) RETURNING id"
                ),
                {
                    "p": world.project.id,
                    "k": world.project.key,
                    "n": n,
                    "t": world.lookups["tracker"],
                    "s": world.lookups["status"],
                    "pr": world.lookups["priority"],
                    "a": world.lookups["author"],
                    "subj": f"{subject} {n}",
                },
            )
        ).scalar_one()
        source_id = (
            await db.execute(
                text(
                    "INSERT INTO search_sources (source_type, entity_id, project_id) "
                    "VALUES ('issue', :e, :p) RETURNING id"
                ),
                {"e": issue_id, "p": world.project.id},
            )
        ).scalar_one()
        chunk_id = (
            await db.execute(
                text("INSERT INTO search_chunks (source_id, chunk_index, content) VALUES (:s, 0, :c) RETURNING id"),
                {"s": source_id, "c": f"{subject} {n}"},
            )
        ).scalar_one()
        await db.execute(
            text("INSERT INTO chunk_embeddings (chunk_id, model_id, embedding) VALUES (:c, :m, CAST(:v AS vector))"),
            {"c": chunk_id, "m": world.model.id, "v": _literal(_at_similarity(similarity, 1 + (n % (DIMS - 1))))},
        )
        ids.append(issue_id)
    await db.commit()
    return ids


async def _semantic(db: AsyncSession, world: World, query: str = "vector query", **kwargs) -> tuple[list[int], int]:
    results, total = await SearchService().semantic_search(
        db, query, world.admin, project_id=world.project.id, resolve_identifiers=False, **kwargs
    )
    return [r.id for r in results], total


# ---------------------------------------------------------------------------
# Semantic search
# ---------------------------------------------------------------------------


async def test_unrelated_query_finds_nothing(db_session: AsyncSession, world: World) -> None:
    await _add_issues(db_session, world, [NOISE, 0.80, 0.70, 0.81], subject="Noise")

    ids, total = await _semantic(db_session, world)

    assert ids == []
    assert total == 0


async def test_related_query_finds_only_chunks_above_the_cut_off(db_session: AsyncSession, world: World) -> None:
    related = await _add_issues(db_session, world, [RELATED, 0.83], subject="Related")
    await _add_issues(db_session, world, [NOISE, 0.81, 0.60], subject="Noise")

    results, total = await SearchService().semantic_search(
        db_session, "vector query", world.admin, project_id=world.project.id, resolve_identifiers=False
    )

    assert [r.id for r in results] == related
    assert total == 2
    # The exposed score is the cosine similarity the cut-off compares against.
    assert [round(r.score, 3) for r in results] == [RELATED, 0.83]


async def test_cut_off_is_inclusive(db_session: AsyncSession, world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "search_semantic_min_similarity", 0.5)
    at_threshold = await _add_issues(db_session, world, [0.5], subject="Edge")
    await _add_issues(db_session, world, [0.49], subject="Below")

    ids, total = await _semantic(db_session, world)

    assert (ids, total) == (at_threshold, 1)


@pytest.mark.parametrize(
    ("configured", "expected_count"),
    [(None, 1), (0.75, 2), (0.9, 0), (-1.0, 4)],
)
async def test_setting_replaces_model_default(
    db_session: AsyncSession,
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    configured: float | None,
    expected_count: int,
) -> None:
    await _add_issues(db_session, world, [RELATED, NOISE, 0.50, -0.20], subject="Mixed")
    monkeypatch.setattr(get_settings(), "search_semantic_min_similarity", configured)

    ids, total = await _semantic(db_session, world)

    assert len(ids) == total == expected_count


async def test_model_without_default_keeps_every_neighbour(db_session: AsyncSession, world: World) -> None:
    """Models with no measured cut-off (like the test mock) rank everything, as before."""
    world.model.model_name = "mock-384"
    await db_session.commit()
    await _add_issues(db_session, world, [RELATED, NOISE, -0.30], subject="Any")

    ids, total = await _semantic(db_session, world)

    assert len(ids) == total == 3


async def test_pagination_total_counts_only_hits_above_the_cut_off(db_session: AsyncSession, world: World) -> None:
    related = await _add_issues(db_session, world, [0.95, 0.93, 0.91, 0.89, 0.87], subject="Related")
    await _add_issues(db_session, world, [NOISE] * 6, subject="Noise")

    first, total = await _semantic(db_session, world, limit=2)
    second, _ = await _semantic(db_session, world, offset=2, limit=2)
    third, _ = await _semantic(db_session, world, offset=4, limit=2)

    assert total == 5
    assert first + second + third == related


# ---------------------------------------------------------------------------
# Vector index path
# ---------------------------------------------------------------------------


async def test_index_rounds_stop_at_the_cut_off(
    db_session: AsyncSession, world: World, caplog: pytest.LogCaptureFixture
) -> None:
    """Once a round reaches chunks below the cut-off, a wider round cannot add hits."""
    related = await _add_issues(db_session, world, [RELATED], subject="Related")
    await _add_issues(db_session, world, [NOISE - i * 0.001 for i in range(60)], subject="Noise")

    await db_session.execute(text("SET LOCAL enable_seqscan = off"))
    await db_session.execute(text("SET LOCAL enable_bitmapscan = off"))
    with caplog.at_level(logging.DEBUG, logger=search_module.__name__):
        ids, _ = await _semantic(db_session, world, limit=10, skip_count=True)

    rounds = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Semantic search round")]
    assert ids == related
    assert rounds == ["Semantic search round: 40 candidates, 1 of 10 results"]


# ---------------------------------------------------------------------------
# Hybrid search
# ---------------------------------------------------------------------------


async def test_hybrid_keeps_keyword_hits_and_drops_semantic_noise(db_session: AsyncSession, world: World) -> None:
    # Keyword hits whose embeddings are far from the query: they stay.
    keyword = await _add_issues(db_session, world, [0.30, NOISE], subject="Gearbox")
    # A semantic hit with no keyword match: it clears the cut-off and stays.
    semantic = await _add_issues(db_session, world, [RELATED], subject="Transmission")
    # Semantic-only noise: dropped.
    noise = await _add_issues(db_session, world, [NOISE, 0.81, 0.79], subject="Unrelated")

    results, total, type_counts = await SearchService().hybrid_search(
        db_session, "gearbox", world.admin, project_id=world.project.id
    )

    ids = [r.id for r in results]
    assert set(ids) == set(keyword) | set(semantic)
    assert not set(ids) & set(noise)
    assert total == len(ids) == type_counts["all"] == 3
    by_id = {r.id: r for r in results}
    assert all(by_id[i].fts_rank is not None for i in keyword)
    assert all(by_id[i].sem_rank is None for i in keyword)
    assert by_id[semantic[0]].sem_rank == 1
    assert by_id[semantic[0]].fts_rank is None


async def test_hybrid_with_no_real_match_is_empty(db_session: AsyncSession, world: World) -> None:
    await _add_issues(db_session, world, [NOISE, 0.80, 0.70], subject="Unrelated")

    results, total, type_counts = await SearchService().hybrid_search(
        db_session, "trash", world.admin, project_id=world.project.id
    )

    assert results == []
    assert total == 0
    assert type_counts["all"] == 0


# ---------------------------------------------------------------------------
# Search API
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["semantic", "hybrid"])
async def test_search_api_unrelated_query_returns_no_results(
    db_session: AsyncSession, client: AsyncClient, world: World, mode: str
) -> None:
    await _add_issues(db_session, world, [NOISE, 0.81], subject="Unrelated")
    resp = await client.post("/api/v1/auth/login/", json={"login": world.admin.login, "password": TEST_PASSWORD})
    assert resp.status_code == 200, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['access_token']}"

    resp = await client.get("/api/v1/search/", params={"q": "trash", "mode": mode, "project_key": world.project.key})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_count"] == 0
    assert body["items"] == []
