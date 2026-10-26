"""Semantic search through the HNSW vector index on ``chunk_embeddings``.

The index (migration 0038) covers 384-dimensional embeddings. Semantic search
reads the nearest chunks from it in rounds and applies visibility afterwards,
widening the round until enough visible results are found, and ranks every
embedding exactly when the index cannot supply them. These tests check that:

- the semantic query is planned as an index scan once the index exists;
- results through the index equal the exact ranking;
- visible results are not lost when many hidden chunks rank ahead of them,
  whether found by a wider index round or by the exact fallback;
- an older pgvector without iterative scans gets the exact ranking;
- hybrid search still fuses keyword and semantic hits through the index.

Rows are inserted with plain SQL so that no indexing events fire, and query
embeddings are fixed vectors, so distances are known in advance.
"""

from __future__ import annotations

import logging
import math
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.constants import SEMANTIC_INDEX_DIMENSIONS
from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.role import Role
from specivo.models.search import EmbeddingModel
from specivo.models.user import User
from specivo.services import search_service as search_module
from specivo.services.embedding_service import EmbeddingService
from specivo.services.search_service import SearchService
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

INDEX_NAME = "ix_chunk_embeddings_hnsw_384"
DIMS = SEMANTIC_INDEX_DIMENSIONS


# ---------------------------------------------------------------------------
# Vectors
# ---------------------------------------------------------------------------


def _unit(values: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in values))
    return [v / norm for v in values]


def _axis(i: int) -> list[float]:
    vec = [0.0] * DIMS
    vec[i] = 1.0
    return vec


def _near(base: list[float], spread: float, rng: random.Random) -> list[float]:
    """A unit vector around *base*; a larger *spread* puts it further away."""
    return _unit([b + rng.gauss(0.0, spread) for b in base])


def _literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{v:.6f}" for v in vec) + "]"


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@dataclass
class World:
    model: EmbeddingModel
    admin: User
    viewer: User
    visible: Project
    hidden: Project
    lookups: dict[str, int]
    query: dict[str, list[float]]


@pytest_asyncio.fixture
async def world(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[World]:
    """Two private projects; ``viewer`` is a member of ``visible`` only."""
    status = StatusFactory.build(name="Vec New")
    db_session.add(status)
    await db_session.flush()
    tracker = TrackerFactory.build(name="Vec Bug", default_status_id=status.id)
    priority = PriorityFactory.build(name="Vec Normal")
    admin = AdminUserFactory.build(login="vecidx_admin", status="active")
    viewer = UserFactory.build(login="vecidx_viewer", status="active")
    visible = ProjectFactory.build(key="VECVIS", identifier="vecidx-visible", is_public=False)
    hidden = ProjectFactory.build(key="VECHID", identifier="vecidx-hidden", is_public=False)
    db_session.add_all([tracker, priority, admin, viewer, visible, hidden])
    await db_session.flush()

    role = Role(name="Vec Viewer", permissions=["view_issues"], builtin=0, issues_visibility="default")
    db_session.add(role)
    await db_session.flush()
    member = Member(user_id=viewer.id, project_id=visible.id)
    db_session.add(member)
    await db_session.flush()
    db_session.add(MemberRole(member_id=member.id, role_id=role.id))

    await db_session.execute(update(EmbeddingModel).where(EmbeddingModel.is_default.is_(True)).values(is_default=False))
    model = EmbeddingModel(name="vecidx-mock", provider="mock", model_name="mock-384", dimensions=DIMS, is_default=True)
    db_session.add(model)
    await db_session.commit()

    query = {"vector": _axis(0)}

    async def fixed_embedding(self, text_: str, model_: EmbeddingModel, intent: str = "passage") -> list[float]:
        return query["vector"]

    monkeypatch.setattr(EmbeddingService, "generate_embedding", fixed_embedding)
    yield World(
        model=model,
        admin=admin,
        viewer=viewer,
        visible=visible,
        hidden=hidden,
        lookups={"tracker": tracker.id, "status": status.id, "priority": priority.id, "author": admin.id},
        query=query,
    )


_seq: dict[int, int] = {}


async def _add_issues(
    db: AsyncSession, world: World, project: Project, vectors: list[list[float]], subject: str = "Vector issue"
) -> list[int]:
    """Insert one issue per vector, each with one chunk embedded as that vector."""
    start = _seq.get(project.id, 0)
    _seq[project.id] = start + len(vectors)
    ids: list[int] = []
    for n, vec in enumerate(vectors, start=start + 1):
        issue_id = (
            await db.execute(
                text(
                    "INSERT INTO issues (project_id, project_key, sequence_number, tracker_id, status_id, "
                    "priority_id, author_id, subject, lock_version) "
                    "VALUES (:p, :k, :n, :t, :s, :pr, :a, :subj, 1) RETURNING id"
                ),
                {
                    "p": project.id,
                    "k": project.key,
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
                {"e": issue_id, "p": project.id},
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
            {"c": chunk_id, "m": world.model.id, "v": _literal(vec)},
        )
        ids.append(issue_id)
    await db.commit()
    return ids


async def _bulk_random_issues(db: AsyncSession, world: World, project: Project, count: int) -> None:
    """Insert *count* issues with one randomly embedded chunk each, entirely in SQL."""
    params = {"p": project.id, "k": project.key, "n": count, "m": world.model.id, **world.lookups}
    await db.execute(
        text(
            "INSERT INTO issues (project_id, project_key, sequence_number, tracker_id, status_id, priority_id, "
            "author_id, subject, lock_version) "
            "SELECT :p, :k, 100000 + g, :tracker, :status, :priority, :author, 'Bulk issue ' || g, 1 "
            "FROM generate_series(1, :n) g"
        ),
        params,
    )
    await db.execute(
        text(
            "INSERT INTO search_sources (source_type, entity_id, project_id) "
            "SELECT 'issue', id, project_id FROM issues WHERE project_id = :p AND sequence_number > 100000"
        ),
        params,
    )
    await db.execute(
        text(
            "INSERT INTO search_chunks (source_id, chunk_index, content) "
            "SELECT ss.id, 0, 'Bulk chunk ' || ss.entity_id FROM search_sources ss WHERE ss.project_id = :p "
            "AND NOT EXISTS (SELECT 1 FROM search_chunks sc WHERE sc.source_id = ss.id)"
        ),
        params,
    )
    await db.execute(
        text(
            "INSERT INTO chunk_embeddings (chunk_id, model_id, embedding) "
            "SELECT sc.id, :m, (SELECT array_agg(random() - 0.5) FROM generate_series(1, 384) WHERE sc.id > 0)::vector "
            "FROM search_chunks sc JOIN search_sources ss ON ss.id = sc.source_id WHERE ss.project_id = :p "
            "AND NOT EXISTS (SELECT 1 FROM chunk_embeddings ce WHERE ce.chunk_id = sc.id)"
        ),
        params,
    )
    await db.commit()


@asynccontextmanager
async def _planner_prefers_indexes(db: AsyncSession) -> AsyncIterator[None]:
    """Make the planner take the HNSW index even for the few rows a test holds."""
    await db.execute(text("SET LOCAL enable_seqscan = off"))
    await db.execute(text("SET LOCAL enable_bitmapscan = off"))
    try:
        yield
    finally:
        await db.execute(text("SET LOCAL enable_seqscan = on"))
        await db.execute(text("SET LOCAL enable_bitmapscan = on"))


async def _search(db: AsyncSession, user: User, limit: int = 10, **kwargs: Any) -> list[tuple[str, int, float]]:
    results, _ = await SearchService().semantic_search(
        db, "vector query", user, limit=limit, skip_count=True, resolve_identifiers=False, **kwargs
    )
    return [(r.result_type, r.id, round(r.score, 5)) for r in results]


async def _exact(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch, user: User, limit: int = 10, **kwargs: Any
) -> list[tuple[str, int, float]]:
    """The same search ranked exactly, without the index."""

    async def no_index(self: SearchService, session: AsyncSession) -> bool:
        return False

    with monkeypatch.context() as patch:
        patch.setattr(SearchService, "_prepare_index_scan", no_index)
        return await _search(db, user, limit=limit, **kwargs)


def _rounds(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("Semantic search round")]


# ---------------------------------------------------------------------------
# The index is in place and used
# ---------------------------------------------------------------------------


async def test_index_exists_and_is_valid(db_session: AsyncSession) -> None:
    row = (
        await db_session.execute(
            text(
                "SELECT i.indisvalid, pg_get_indexdef(i.indexrelid) FROM pg_index i "
                "WHERE i.indexrelid = to_regclass(:name)"
            ),
            {"name": INDEX_NAME},
        )
    ).one()
    assert row[0] is True
    assert "hnsw" in row[1]
    assert "vector_cosine_ops" in row[1]
    assert "vector_dims(embedding) = 384" in row[1]


@pytest.mark.serial
async def test_semantic_query_plans_an_hnsw_index_scan(
    db_session: AsyncSession, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With realistic statistics the planner itself picks the index for the candidate round.

    Serial: the index is dropped for the bulk insert and rebuilt afterwards
    (building is far quicker than 20,000 single inserts into the graph), which
    locks ``chunk_embeddings`` until the test's transaction rolls back.
    """
    index_def = (
        await db_session.execute(text("SELECT pg_get_indexdef(to_regclass(:name))"), {"name": INDEX_NAME})
    ).scalar_one()
    await db_session.execute(text(f"DROP INDEX {INDEX_NAME}"))
    await _bulk_random_issues(db_session, world, world.visible, 20_000)
    await db_session.execute(text(index_def))
    await db_session.execute(text("ANALYZE chunk_embeddings"))

    captured: list[tuple[str, dict[str, Any]]] = []
    original = db_session.execute

    async def spy(statement: Any, params: Any = None, *args: Any, **kwargs: Any) -> Any:
        if "candidate_count" in str(statement):
            captured.append((str(statement), dict(params)))
        return await original(statement, params, *args, **kwargs)

    monkeypatch.setattr(db_session, "execute", spy)
    world.query["vector"] = _unit([random.Random(7).gauss(0.0, 1.0) for _ in range(DIMS)])
    results = await _search(db_session, world.admin, limit=10)
    assert len(results) == 10

    assert captured, "the semantic query did not run"
    sql, params = captured[0]
    assert params["candidates"] is not None, "the first round must go through the index"
    plan = "\n".join((await original(text("EXPLAIN " + sql), params)).scalars().all())
    assert f"Index Scan using {INDEX_NAME}" in plan, plan


async def test_exact_ranking_does_not_use_the_index(
    db_session: AsyncSession, world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A model of another size has no index: one exact round, still correct."""
    world.model.dimensions = 8
    await db_session.commit()
    world.query["vector"] = _unit([1.0] + [0.0] * 7)
    rng = random.Random(3)
    await _add_issues(db_session, world, world.visible, [_near(world.query["vector"], 0.3, rng) for _ in range(5)])

    with caplog.at_level(logging.DEBUG, logger=search_module.__name__):
        results = await _search(db_session, world.admin, limit=10)
    assert len(results) == 5
    assert _rounds(caplog) == ["Semantic search round: all candidates, 5 of 10 results"]


# ---------------------------------------------------------------------------
# Correctness against the exact ranking
# ---------------------------------------------------------------------------


async def test_index_results_equal_exact_ranking(
    db_session: AsyncSession, world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rng = random.Random(11)
    await _add_issues(db_session, world, world.visible, [_near(_axis(0), 0.08, rng) for _ in range(150)])
    await _add_issues(db_session, world, world.hidden, [_near(_axis(0), 0.08, rng) for _ in range(150)])

    for user in (world.admin, world.viewer):
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger=search_module.__name__):
            async with _planner_prefers_indexes(db_session):
                via_index = await _search(db_session, user, limit=25)
        assert _rounds(caplog)[0].startswith("Semantic search round: 100 candidates")
        assert "all candidates" not in _rounds(caplog)[-1]
        assert via_index == await _exact(db_session, monkeypatch, user, limit=25)
        assert len(via_index) == 25

    # The viewer only ever sees the project they belong to.
    viewer_ids = {issue_id for _, issue_id, _ in await _search(db_session, world.viewer, limit=50)}
    hidden_ids = set(
        (
            await db_session.execute(text("SELECT id FROM issues WHERE project_id = :p"), {"p": world.hidden.id})
        ).scalars()
    )
    assert viewer_ids and not viewer_ids & hidden_ids


async def test_deeper_pages_and_project_scope_match_exact_ranking(
    db_session: AsyncSession, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    rng = random.Random(5)
    await _add_issues(db_session, world, world.visible, [_near(_axis(0), 0.08, rng) for _ in range(120)])
    await _add_issues(db_session, world, world.hidden, [_near(_axis(0), 0.08, rng) for _ in range(120)])

    async with _planner_prefers_indexes(db_session):
        page_two = await _search(db_session, world.admin, limit=10, offset=10)
        scoped = await _search(db_session, world.admin, limit=10, project_id=world.hidden.id)
    assert page_two == await _exact(db_session, monkeypatch, world.admin, limit=10, offset=10)
    assert scoped == await _exact(db_session, monkeypatch, world.admin, limit=10, project_id=world.hidden.id)
    hidden_ids = set(
        (
            await db_session.execute(text("SELECT id FROM issues WHERE project_id = :p"), {"p": world.hidden.id})
        ).scalars()
    )
    assert {issue_id for _, issue_id, _ in scoped} <= hidden_ids


# ---------------------------------------------------------------------------
# Visible results behind many hidden ones
# ---------------------------------------------------------------------------


async def test_wider_index_round_finds_visible_results_behind_hidden_ones(
    db_session: AsyncSession, world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The first round holds only hidden chunks; a wider index round reaches the visible ones."""
    rng = random.Random(13)
    hidden_ids = await _add_issues(db_session, world, world.hidden, [_near(_axis(0), 0.01, rng) for _ in range(60)])
    visible_ids = await _add_issues(db_session, world, world.visible, [_near(_axis(0), 0.08, rng) for _ in range(400)])

    with caplog.at_level(logging.DEBUG, logger=search_module.__name__):
        async with _planner_prefers_indexes(db_session):
            via_index = await _search(db_session, world.viewer, limit=10)

    assert _rounds(caplog) == [
        "Semantic search round: 40 candidates, 0 of 10 results",
        "Semantic search round: 640 candidates, 10 of 10 results",
    ]
    ids = [issue_id for _, issue_id, _ in via_index]
    assert len(ids) == 10
    assert set(ids) <= set(visible_ids)
    assert not set(ids) & set(hidden_ids)
    assert via_index == await _exact(db_session, monkeypatch, world.viewer, limit=10)


async def test_exact_fallback_finds_visible_results_when_the_index_runs_out(
    db_session: AsyncSession, world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Fewer visible chunks than wanted: the index runs dry and the exact ranking takes over."""
    rng = random.Random(17)
    await _add_issues(db_session, world, world.hidden, [_near(_axis(0), 0.01, rng) for _ in range(200)])
    visible_ids = await _add_issues(db_session, world, world.visible, [_near(_axis(0), 0.08, rng) for _ in range(6)])

    with caplog.at_level(logging.DEBUG, logger=search_module.__name__):
        async with _planner_prefers_indexes(db_session):
            results = await _search(db_session, world.viewer, limit=10)

    rounds = _rounds(caplog)
    assert rounds[0] == "Semantic search round: 40 candidates, 0 of 10 results"
    assert rounds[-1] == "Semantic search round: all candidates, 6 of 10 results"
    assert sorted(issue_id for _, issue_id, _ in results) == sorted(visible_ids)
    assert results == await _exact(db_session, monkeypatch, world.viewer, limit=10)


async def test_candidate_cap_switches_to_exact_ranking(
    db_session: AsyncSession, world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(search_module, "SEMANTIC_MAX_INDEX_CANDIDATES", 100)
    rng = random.Random(19)
    await _add_issues(db_session, world, world.hidden, [_near(_axis(0), 0.01, rng) for _ in range(150)])
    visible_ids = await _add_issues(db_session, world, world.visible, [_near(_axis(0), 0.08, rng) for _ in range(300)])

    with caplog.at_level(logging.DEBUG, logger=search_module.__name__):
        async with _planner_prefers_indexes(db_session):
            results = await _search(db_session, world.viewer, limit=10)

    assert _rounds(caplog) == [
        "Semantic search round: 40 candidates, 0 of 10 results",
        "Semantic search round: all candidates, 10 of 10 results",
    ]
    assert {issue_id for _, issue_id, _ in results} <= set(visible_ids)
    assert results == await _exact(db_session, monkeypatch, world.viewer, limit=10)


# ---------------------------------------------------------------------------
# Settings and pgvector versions
# ---------------------------------------------------------------------------


async def test_index_round_sets_ef_search_and_iterative_scan(
    db_session: AsyncSession, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from specivo.core.config import get_settings

    monkeypatch.setattr(get_settings(), "search_hnsw_ef_search", 64)
    await _add_issues(db_session, world, world.visible, [_axis(0)])
    await _search(db_session, world.admin)

    ef_search = (await db_session.execute(text("SHOW hnsw.ef_search"))).scalar_one()
    iterative = (await db_session.execute(text("SHOW hnsw.iterative_scan"))).scalar_one()
    assert (ef_search, iterative) == ("64", "relaxed_order")


async def test_old_pgvector_ranks_exactly(
    db_session: AsyncSession, world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Without iterative scans the index cannot feed the rounds, so the ranking is exact."""
    monkeypatch.setattr(search_module, "_pgvector_iterative_scan", False)
    rng = random.Random(23)
    await _add_issues(db_session, world, world.visible, [_near(_axis(0), 0.08, rng) for _ in range(30)])

    with caplog.at_level(logging.DEBUG, logger=search_module.__name__):
        results = await _search(db_session, world.admin, limit=10)
    assert _rounds(caplog) == ["Semantic search round: all candidates, 10 of 10 results"]
    assert len(results) == 10


@pytest.mark.parametrize(
    ("version", "supported"),
    [("0.8.0", True), ("0.8.2", True), ("0.10.1", True), ("0.7.4", False), ("0.5.1", False)],
)
async def test_pgvector_version_gate(version: str, supported: bool) -> None:
    from specivo.core.constants import PGVECTOR_ITERATIVE_SCAN_VERSION

    assert (search_module._parse_extversion(version) >= PGVECTOR_ITERATIVE_SCAN_VERSION) is supported


# ---------------------------------------------------------------------------
# Hybrid search
# ---------------------------------------------------------------------------


async def test_hybrid_search_fuses_index_hits_with_keyword_hits(
    db_session: AsyncSession, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    rng = random.Random(29)
    hidden_ids = await _add_issues(
        db_session, world, world.hidden, [_near(_axis(0), 0.01, rng) for _ in range(80)], subject="Hidden gearbox"
    )
    semantic_ids = await _add_issues(
        db_session, world, world.visible, [_near(_axis(0), 0.08, rng) for _ in range(40)], subject="Semantic neighbour"
    )
    keyword_ids = await _add_issues(
        db_session, world, world.visible, [_near(_axis(1), 0.08, rng) for _ in range(3)], subject="Gearbox"
    )

    async with _planner_prefers_indexes(db_session):
        results, total, _ = await SearchService().hybrid_search(db_session, "gearbox", world.viewer, limit=25)

    ids = [r.id for r in results]
    assert total >= len(ids) == 25
    assert not set(ids) & set(hidden_ids)
    # Keyword hits (FTS on the subject) and semantic neighbours both reach the page.
    assert set(keyword_ids) & set(ids)
    assert set(semantic_ids) & set(ids)
    assert any(r.sem_rank is not None for r in results)
    assert any(r.fts_rank is not None for r in results)
