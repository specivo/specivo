"""Integration tests for v0.17.0 search fixes (#178-#181).

RED phase — all tests should FAIL until the corresponding fixes are implemented.

Covers:
- Deny-by-default visibility (unknown source_type excluded from semantic search)
- Multi-model HNSW correctness (semantic search filters by model_id)
- Hybrid concurrent correctness (hybrid returns fused results from both FTS and semantic)
- Medium fixes (backfill large batch, semantic dedup returns unique entities)
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.member import Member, MemberRole
from specivo.models.project import EnabledModule, Project
from specivo.models.role import Role
from specivo.models.search import ChunkEmbedding, EmbeddingModel, SearchChunk, SearchSource
from specivo.models.user import User
from specivo.services.embedding_service import EmbeddingService
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import TEST_PASSWORD, UserFactory

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SEARCH_URL = "/api/v1/search"


async def _make_user(db: AsyncSession, login: str = "fix_user") -> User:
    user = UserFactory.build(login=login, status="active")
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _login(client: AsyncClient, login: str) -> str:
    resp = await client.post(
        "/api/v1/auth/login",
        json={"login": login, "password": TEST_PASSWORD},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


async def _make_project(db: AsyncSession, key: str = "FIX", identifier: str = "fix-project") -> Project:
    proj = ProjectFactory.build(key=key, identifier=identifier)
    db.add(proj)
    await db.commit()
    await db.refresh(proj)
    return proj


async def _seed_lookups(
    db: AsyncSession,
) -> tuple[Tracker, IssueStatus, IssuePriority]:
    status = StatusFactory.build(name="New", position=1, is_closed=False)
    db.add(status)
    await db.flush()
    tracker = TrackerFactory.build(name="Bug", default_status_id=status.id)
    db.add(tracker)
    priority = PriorityFactory.build(name="Normal", is_default=True, position=2)
    db.add(priority)
    await db.commit()
    await db.refresh(status)
    await db.refresh(tracker)
    await db.refresh(priority)
    return tracker, status, priority


async def _enable_wiki(db: AsyncSession, project: Project) -> None:
    db.add(EnabledModule(project_id=project.id, name="wiki"))
    await db.commit()


async def _add_manager(db: AsyncSession, project: Project, user: User) -> None:
    role = Role(
        name=f"Manager-{project.key}-{user.id}",
        permissions=["*"],
        builtin=0,
    )
    db.add(role)
    await db.flush()
    member = Member(user_id=user.id, project_id=project.id)
    db.add(member)
    await db.flush()
    mr = MemberRole(member_id=member.id, role_id=role.id)
    db.add(mr)
    await db.commit()


async def _create_mock_model(
    db: AsyncSession,
    name: str = "test-mock",
    model_name: str = "mock-1536",
    is_default: bool = True,
) -> EmbeddingModel:
    """Create a mock embedding model for tests."""
    model = EmbeddingModel(
        name=name,
        provider="mock",
        model_name=model_name,
        dimensions=1536,
        is_default=is_default,
    )
    db.add(model)
    await db.commit()
    await db.refresh(model)
    return model


async def _create_issue(
    client: AsyncClient,
    project_key: str,
    tracker_id: int,
    subject: str,
    description: str | None = None,
) -> dict:
    payload: dict = {
        "project_key": project_key,
        "tracker_id": tracker_id,
        "subject": subject,
    }
    if description is not None:
        payload["description"] = description
    resp = await client.post(f"/api/v1/projects/{project_key}/issues", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _embed_source_directly(
    db: AsyncSession,
    source_type: str,
    entity_id: int,
    project_id: int,
    chunks: list[str],
    model_id: int | None = None,
) -> SearchSource | None:
    """Embed a source entity directly via EmbeddingService (bypasses API)."""
    svc = EmbeddingService()
    return await svc.embed_source(db, source_type, entity_id, project_id, chunks, model_id)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def project(db_session: AsyncSession) -> Project:
    return await _make_project(db_session)


@pytest_asyncio.fixture
async def lookups(db_session: AsyncSession) -> tuple[Tracker, IssueStatus, IssuePriority]:
    return await _seed_lookups(db_session)


@pytest_asyncio.fixture
async def search_user(db_session: AsyncSession) -> User:
    return await _make_user(db_session, login="fix_test_user")


@pytest_asyncio.fixture
async def mock_model(db_session: AsyncSession) -> EmbeddingModel:
    return await _create_mock_model(db_session)


@pytest_asyncio.fixture
async def authed_client(
    db_session: AsyncSession,
    client: AsyncClient,
    project: Project,
    search_user: User,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
    mock_model: EmbeddingModel,
) -> AsyncClient:
    """Client authenticated as a manager with wiki module enabled and mock embedding model."""
    await _enable_wiki(db_session, project)
    await _add_manager(db_session, project, search_user)
    token = await _login(client, search_user.login)
    client.headers["Authorization"] = f"Bearer {token}"
    return client


# ---------------------------------------------------------------------------
# Deny-by-default visibility
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_source_type_excluded_from_semantic_search(
    authed_client: AsyncClient,
    db_session: AsyncSession,
    project: Project,
    mock_model: EmbeddingModel,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
):
    """SearchSource with source_type='unknown' must NOT appear in semantic results.

    The deny-by-default fix means only explicitly allowed source types
    (issue, wiki_page, comment) are returned. Any other source_type is excluded
    even if it has valid embeddings.
    """
    tracker, _, _ = lookups

    # Create a normal issue so there is at least one valid result
    await _create_issue(
        authed_client,
        project.key,
        tracker.id,
        "Quantum flux capacitor design",
        description="Advanced quantum computing architecture for flux operations",
    )

    # Directly insert a SearchSource with an unknown source type and embed it
    source = await _embed_source_directly(
        db_session,
        source_type="unknown",
        entity_id=99999,
        project_id=project.id,
        chunks=["Quantum flux capacitor design advanced quantum computing"],
        model_id=mock_model.id,
    )
    await db_session.commit()
    assert source is not None

    # Semantic search should NOT return the unknown source type
    resp = await authed_client.get(
        SEARCH_URL,
        params={"q": "quantum flux capacitor", "mode": "semantic"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    result_types = {item["result_type"] for item in data["items"]}
    assert "unknown" not in result_types, (
        "Unknown source types must be excluded from semantic search results (deny-by-default)"
    )


# ---------------------------------------------------------------------------
# Multi-model HNSW (correctness only)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_semantic_search_filters_by_model_id(
    authed_client: AsyncClient,
    db_session: AsyncSession,
    project: Project,
    mock_model: EmbeddingModel,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
):
    """Semantic search with model A must not return embeddings from model B.

    When multiple models exist, the search must use only the default model's
    embeddings. Results from a non-default model must not leak through.
    """
    tracker, _, _ = lookups

    # Create an issue that gets embedded with the default mock model
    await _create_issue(
        authed_client,
        project.key,
        tracker.id,
        "Photosynthesis regulation mechanism",
        description="Study of chloroplast regulation in plant cells",
    )

    # Create a second non-default model
    model_b = await _create_mock_model(
        db_session,
        name="test-mock-b",
        model_name="mock-768",
        is_default=False,
    )

    # Manually create a SearchSource + chunk + embedding ONLY for model B
    # with completely different content that should not match if model filtering works
    source = SearchSource(
        source_type="issue",
        entity_id=88888,
        project_id=project.id,
    )
    db_session.add(source)
    await db_session.flush()

    chunk = SearchChunk(
        source_id=source.id,
        chunk_index=0,
        content="Photosynthesis regulation mechanism in alien biology",
        metadata_json={"source_type": "issue", "entity_id": 88888},
    )
    db_session.add(chunk)
    await db_session.flush()

    # Generate embedding with model B only
    emb_svc = EmbeddingService()
    vector = await emb_svc.generate_embedding(chunk.content, model_b)
    embedding = ChunkEmbedding(
        chunk_id=chunk.id,
        model_id=model_b.id,
        embedding=vector,
    )
    db_session.add(embedding)
    await db_session.commit()

    # Semantic search uses the default model (mock_model) — entity 88888 has
    # no embedding for the default model, so it must not appear
    resp = await authed_client.get(
        SEARCH_URL,
        params={"q": "photosynthesis regulation", "mode": "semantic"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    result_ids = {item["id"] for item in data["items"]}
    assert 88888 not in result_ids, (
        "Entity with embeddings only for a non-default model must not appear in semantic results"
    )


# ---------------------------------------------------------------------------
# Hybrid concurrent correctness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hybrid_search_returns_fused_results(
    authed_client: AsyncClient,
    db_session: AsyncSession,
    project: Project,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
    mock_model: EmbeddingModel,
):
    """Hybrid search returns results from BOTH FTS and semantic branches.

    After the fix, hybrid mode must include results that appear in
    either FTS or semantic results (or both), fused via RRF.
    """
    tracker, _, _ = lookups

    # Create issues with distinct text so at least some appear in both FTS and semantic
    await _create_issue(
        authed_client,
        project.key,
        tracker.id,
        "Thermodynamic equilibrium analysis",
        description="Study of entropy and enthalpy in closed systems",
    )
    await _create_issue(
        authed_client,
        project.key,
        tracker.id,
        "Entropy calculation methods",
        description="Various approaches to computing thermodynamic entropy",
    )

    # Run hybrid search
    resp = await authed_client.get(
        SEARCH_URL,
        params={"q": "thermodynamic entropy", "mode": "hybrid"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    # Hybrid must return at least 1 result (FTS and semantic both match)
    assert data["total_count"] >= 1, "Hybrid search must return fused results from FTS + semantic"
    assert len(data["items"]) >= 1

    # Verify that results are ranked (score > 0 and decreasing)
    scores = [item["score"] for item in data["items"]]
    assert all(s > 0 for s in scores), "All hybrid scores must be positive"
    assert scores == sorted(scores, reverse=True), "Results must be sorted by score descending"


# ---------------------------------------------------------------------------
# Medium fixes: backfill large batch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_handles_large_batch(
    db_session: AsyncSession,
    project: Project,
    mock_model: EmbeddingModel,
):
    """Backfill with 100+ chunks must complete without OOM or error.

    The fix introduces batched processing so that backfilling a large
    number of chunks does not load all into memory at once.
    """
    emb_svc = EmbeddingService()

    # Create 120 chunks (above the batch threshold) with only model A embeddings
    for i in range(120):
        source = SearchSource(
            source_type="issue",
            entity_id=70000 + i,
            project_id=project.id,
        )
        db_session.add(source)
        await db_session.flush()

        chunk = SearchChunk(
            source_id=source.id,
            chunk_index=0,
            content=f"Backfill test chunk number {i} with unique content about topic {i}",
            metadata_json={"source_type": "issue", "entity_id": 70000 + i},
        )
        db_session.add(chunk)
        await db_session.flush()

        # Add embedding for the default model
        vector = await emb_svc.generate_embedding(chunk.content, mock_model)
        embedding = ChunkEmbedding(
            chunk_id=chunk.id,
            model_id=mock_model.id,
            embedding=vector,
        )
        db_session.add(embedding)

    await db_session.commit()

    # Create a second model for backfill
    model_b = EmbeddingModel(
        name="backfill-large-test",
        provider="mock",
        model_name="mock-1536-backfill",
        dimensions=1536,
        is_default=False,
    )
    db_session.add(model_b)
    await db_session.commit()
    await db_session.refresh(model_b)

    # Backfill should handle 120 chunks without error
    count = await emb_svc.backfill_model(db_session, model_b.id)
    await db_session.commit()

    assert count >= 120, f"Backfill must process all 120+ chunks, got {count}"

    # Verify embeddings actually exist for the new model
    result = await db_session.execute(select(ChunkEmbedding).where(ChunkEmbedding.model_id == model_b.id))
    new_embeddings = result.scalars().all()
    assert len(new_embeddings) >= 120


# ---------------------------------------------------------------------------
# Medium fixes: semantic dedup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_semantic_dedup_returns_unique_entities(
    authed_client: AsyncClient,
    db_session: AsyncSession,
    project: Project,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
    mock_model: EmbeddingModel,
):
    """Same entity with multiple chunks must appear only once in semantic results.

    When an issue has multiple search chunks (e.g. long description split into
    sections), semantic search must deduplicate by (source_type, entity_id)
    and return only the highest-scoring chunk per entity.
    """
    tracker, _, _ = lookups

    # Create an issue via API (gets one chunk)
    issue = await _create_issue(
        authed_client,
        project.key,
        tracker.id,
        "Bioluminescence research methodology",
        description="Comprehensive study of bioluminescent organisms in deep ocean",
    )

    # Find the existing SearchSource for this issue
    result = await db_session.execute(
        select(SearchSource).where(
            SearchSource.source_type == "issue",
            SearchSource.project_id == project.id,
        )
    )
    sources = result.scalars().all()
    assert len(sources) >= 1
    source = sources[0]

    # Add a second chunk to the same source (simulating multi-chunk entity)
    emb_svc = EmbeddingService()
    extra_chunk = SearchChunk(
        source_id=source.id,
        chunk_index=1,
        content="Bioluminescence research additional findings on deep ocean organisms",
        metadata_json={"source_type": "issue", "entity_id": source.entity_id},
    )
    db_session.add(extra_chunk)
    await db_session.flush()

    vector = await emb_svc.generate_embedding(extra_chunk.content, mock_model)
    embedding = ChunkEmbedding(
        chunk_id=extra_chunk.id,
        model_id=mock_model.id,
        embedding=vector,
    )
    db_session.add(embedding)
    await db_session.commit()

    # Search — the issue should appear only ONCE despite having 2 chunks
    resp = await authed_client.get(
        SEARCH_URL,
        params={"q": "bioluminescence deep ocean", "mode": "semantic"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    # Count how many times this entity appears
    entity_ids = [item["id"] for item in data["items"] if item["result_type"] == "issue"]
    entity_id = source.entity_id
    occurrences = entity_ids.count(entity_id)
    assert occurrences <= 1, (
        f"Entity {entity_id} appears {occurrences} times in results; dedup must ensure each entity appears at most once"
    )
