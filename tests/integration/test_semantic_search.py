"""Integration tests for semantic search with pgvector.

Covers:
- Admin embedding model CRUD
- Embedding generation on issue/wiki create (mock provider)
- Hybrid search (RRF fusion of FTS + semantic)
- Search mode parameter (keyword, semantic, hybrid)
- Pagination, project scoping
- Backfill embeddings for new model
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
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import TEST_PASSWORD, UserFactory

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SEARCH_URL = "/api/v1/search"
ADMIN_MODELS_URL = "/api/v1/admin/embedding-models"


async def _make_user(db: AsyncSession, login: str = "sem_user") -> User:
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


async def _make_project(db: AsyncSession, key: str = "SEM", identifier: str = "semantic-project") -> Project:
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


async def _create_mock_model(db: AsyncSession) -> EmbeddingModel:
    """Create a mock embedding model for tests."""
    model = EmbeddingModel(
        name="test-mock",
        provider="mock",
        model_name="mock-1536",
        dimensions=1536,
        is_default=True,
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


async def _create_wiki_page(
    client: AsyncClient,
    project_key: str,
    title: str,
    page_text: str,
) -> dict:
    resp = await client.post(
        f"/api/v1/projects/{project_key}/wiki",
        json={"title": title, "text": page_text},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


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
    return await _make_user(db_session, login="semantic_test_user")


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
# Admin Embedding Model Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_embedding_model(admin_client: AsyncClient):
    """Admin can create a new embedding model."""
    resp = await admin_client.post(
        ADMIN_MODELS_URL,
        json={
            "name": "test-openai",
            "provider": "openai",
            "model_name": "text-embedding-3-small",
            "dimensions": 1536,
            "is_default": False,
        },
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["name"] == "test-openai"
    assert data["provider"] == "openai"
    assert data["dimensions"] == 1536


@pytest.mark.asyncio
async def test_list_embedding_models(admin_client: AsyncClient, db_session: AsyncSession):
    """Admin can list embedding models."""
    await _create_mock_model(db_session)
    resp = await admin_client.get(ADMIN_MODELS_URL)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) >= 1


@pytest.mark.asyncio
async def test_delete_embedding_model(admin_client: AsyncClient, db_session: AsyncSession):
    """Admin can delete an embedding model."""
    model = await _create_mock_model(db_session)
    resp = await admin_client.delete(f"{ADMIN_MODELS_URL}/{model.id}")
    assert resp.status_code == 204, resp.text


# ---------------------------------------------------------------------------
# Embedding Generation Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embed_issue_on_create(
    authed_client: AsyncClient,
    db_session: AsyncSession,
    project: Project,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
    mock_model: EmbeddingModel,
):
    """Creating an issue generates search chunks and embeddings (mock provider)."""
    tracker, _, _ = lookups
    await _create_issue(
        authed_client,
        project.key,
        tracker.id,
        "Implement user authentication",
        description="Add JWT-based login with refresh tokens",
    )

    # Verify search source was created
    result = await db_session.execute(
        select(SearchSource).where(
            SearchSource.source_type == "issue",
            SearchSource.project_id == project.id,
        )
    )
    sources = result.scalars().all()
    assert len(sources) >= 1

    # Verify chunks were created
    source = sources[0]
    result = await db_session.execute(select(SearchChunk).where(SearchChunk.source_id == source.id))
    chunks = result.scalars().all()
    assert len(chunks) >= 1
    assert "authentication" in chunks[0].content.lower()

    # Verify embeddings were generated
    result = await db_session.execute(
        select(ChunkEmbedding).where(
            ChunkEmbedding.chunk_id == chunks[0].id,
            ChunkEmbedding.model_id == mock_model.id,
        )
    )
    embeddings = result.scalars().all()
    assert len(embeddings) == 1
    assert len(embeddings[0].embedding) == 1536


@pytest.mark.asyncio
async def test_hybrid_search(
    authed_client: AsyncClient,
    db_session: AsyncSession,
    project: Project,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
    mock_model: EmbeddingModel,
):
    """Hybrid search finds issues using RRF fusion of FTS + semantic."""
    tracker, _, _ = lookups
    await _create_issue(
        authed_client,
        project.key,
        tracker.id,
        "Database connection pooling",
        description="Optimize PostgreSQL connection pool settings for production",
    )
    await _create_issue(
        authed_client,
        project.key,
        tracker.id,
        "Cache invalidation strategy",
        description="Design Redis cache eviction policies",
    )

    resp = await authed_client.get(
        SEARCH_URL,
        params={"q": "database connection", "mode": "hybrid"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total_count"] >= 1
    # The database-related issue should appear in results
    subtitles = [item.get("subtitle", "") or item.get("title", "") for item in data["items"]]
    assert any("connection" in s.lower() or "database" in s.lower() for s in subtitles)


@pytest.mark.asyncio
async def test_hybrid_search_pagination(
    authed_client: AsyncClient,
    db_session: AsyncSession,
    project: Project,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
    mock_model: EmbeddingModel,
):
    """Hybrid search respects offset/limit pagination."""
    tracker, _, _ = lookups
    for i in range(5):
        await _create_issue(
            authed_client,
            project.key,
            tracker.id,
            f"Pagination test item {i}",
            description=f"Testing pagination with item number {i}",
        )

    resp = await authed_client.get(
        SEARCH_URL,
        params={"q": "pagination", "mode": "hybrid", "limit": 2, "offset": 0},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data["items"]) <= 2
    assert data["total_count"] >= 5


@pytest.mark.asyncio
async def test_hybrid_search_project_scope(
    authed_client: AsyncClient,
    db_session: AsyncSession,
    project: Project,
    search_user: User,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
    mock_model: EmbeddingModel,
):
    """Hybrid search results are scoped to the specified project."""
    tracker, _, _ = lookups

    # Create second project
    proj2 = await _make_project(db_session, key="OTH", identifier="other-sem-project")
    await _add_manager(db_session, proj2, search_user)

    await _create_issue(authed_client, project.key, tracker.id, "Scoping test in project one")
    await _create_issue(authed_client, proj2.key, tracker.id, "Scoping test in project two")

    resp = await authed_client.get(
        SEARCH_URL,
        params={"q": "scoping", "mode": "hybrid", "project_key": project.key},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # All results should be from the specified project
    for item in data["items"]:
        assert item["project_key"] == project.key


@pytest.mark.asyncio
async def test_backfill_new_model(
    authed_client: AsyncClient,
    db_session: AsyncSession,
    project: Project,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
    mock_model: EmbeddingModel,
):
    """Adding a new model and running backfill generates embeddings for all existing chunks."""
    tracker, _, _ = lookups
    await _create_issue(authed_client, project.key, tracker.id, "Existing issue for backfill")

    # Create a second model
    model2 = EmbeddingModel(
        name="test-mock-2",
        provider="mock",
        model_name="mock-1536-v2",
        dimensions=1536,
        is_default=False,
    )
    db_session.add(model2)
    await db_session.commit()
    await db_session.refresh(model2)

    # Run backfill via embedding service
    from specivo.services.embedding_service import EmbeddingService

    svc = EmbeddingService()
    await svc.backfill_model(db_session, model2.id)
    await db_session.commit()

    # Verify new embeddings exist for the second model
    result = await db_session.execute(select(ChunkEmbedding).where(ChunkEmbedding.model_id == model2.id))
    embeddings = result.scalars().all()
    assert len(embeddings) >= 1


@pytest.mark.asyncio
async def test_search_mode_keyword(
    authed_client: AsyncClient,
    db_session: AsyncSession,
    project: Project,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
    mock_model: EmbeddingModel,
):
    """mode=keyword returns FTS-only results (existing behavior)."""
    tracker, _, _ = lookups
    await _create_issue(authed_client, project.key, tracker.id, "Keyword search test issue")

    resp = await authed_client.get(
        SEARCH_URL,
        params={"q": "keyword", "mode": "keyword"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total_count"] >= 1


@pytest.mark.asyncio
async def test_search_mode_semantic(
    authed_client: AsyncClient,
    db_session: AsyncSession,
    project: Project,
    lookups: tuple[Tracker, IssueStatus, IssuePriority],
    mock_model: EmbeddingModel,
):
    """mode=semantic returns vector-only results."""
    tracker, _, _ = lookups
    await _create_issue(authed_client, project.key, tracker.id, "Semantic only search test")

    resp = await authed_client.get(
        SEARCH_URL,
        params={"q": "semantic search", "mode": "semantic"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # Semantic search should return results (mock embeddings are hash-based)
    assert isinstance(data["items"], list)
