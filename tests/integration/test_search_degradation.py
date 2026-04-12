"""Integration tests for search graceful degradation when embedding model is unavailable.

Verifies that:
- Keyword search works without any embedding model in the DB
- Hybrid search falls back to keyword results (not an error) when model files are missing
- The search API always returns HTTP 200, never HTTP 500, regardless of embedding state

These tests rely on the transaction-rollback isolation strategy, so no embedding
model records are present in the DB by default.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.role import Role
from specivo.models.user import User
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import TEST_PASSWORD, UserFactory

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SEARCH_URL = "/api/v1/search/"


async def _make_user(db: AsyncSession, login: str = "degradation_user") -> User:
    user = UserFactory.build(login=login, status="active")
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _login(client: AsyncClient, login: str) -> str:
    resp = await client.post(
        "/api/v1/auth/login/",
        json={"login": login, "password": TEST_PASSWORD},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


async def _make_project(db: AsyncSession, key: str = "DGR", identifier: str = "degradation-project") -> Project:
    proj = ProjectFactory.build(key=key, identifier=identifier)
    db.add(proj)
    await db.commit()
    await db.refresh(proj)
    return proj


async def _seed_lookups(db: AsyncSession) -> dict:
    status = StatusFactory.build(name="New", position=1, category="backlog")
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
    return {"tracker": tracker, "status": status, "priority": priority}


async def _add_manager(db: AsyncSession, project: Project, user: User) -> None:
    role = Role(
        name=f"Mgr-{project.key}-{user.id}",
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
    resp = await client.post(f"/api/v1/projects/{project_key}/issues/", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def setup_project(db_session: AsyncSession, client: AsyncClient):
    """Seed a project, user, lookups, and return an authenticated client."""
    lookups = await _seed_lookups(db_session)
    project = await _make_project(db_session)
    user = await _make_user(db_session, "degradation_test_user")
    await _add_manager(db_session, project, user)
    token = await _login(client, user.login)
    client.headers["Authorization"] = f"Bearer {token}"
    return {"client": client, "project": project, "tracker": lookups["tracker"]}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_keyword_search_works_without_embedding_model(
    setup_project: dict,
):
    """Keyword search returns results even when no EmbeddingModel exists in the DB.

    The transaction-rollback isolation means no embedding_models rows exist.
    mode=keyword must succeed using only tsvector FTS.
    """
    data = setup_project
    authed_client: AsyncClient = data["client"]
    project: Project = data["project"]
    tracker = data["tracker"]

    await _create_issue(authed_client, project.key, tracker.id, "Embedding degradation test issue")

    resp = await authed_client.get(
        SEARCH_URL,
        params={"q": "degradation", "mode": "keyword"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "items" in body
    assert "total_count" in body
    assert body["total_count"] >= 1


@pytest.mark.asyncio
async def test_hybrid_search_falls_back_to_keyword_when_model_unavailable(
    setup_project: dict,
):
    """Hybrid search returns keyword results (not a 500) when local model files are missing.

    Simulates the production state where embedding_models table has a 'local'
    provider record but the ONNX files are absent from data/models/.
    The search must return keyword results gracefully, not raise.
    """

    data = setup_project
    authed_client: AsyncClient = data["client"]
    project: Project = data["project"]
    tracker = data["tracker"]

    await _create_issue(authed_client, project.key, tracker.id, "Hybrid fallback search issue")

    # Simulate local embedder being unavailable (model files missing)
    with patch(
        "specivo.services.embedding_service.get_local_embedder",
    ) as mock_get_embedder:
        from unittest.mock import MagicMock

        unavailable_embedder = MagicMock()
        unavailable_embedder.is_available.return_value = False
        mock_get_embedder.return_value = unavailable_embedder

        resp = await authed_client.get(
            SEARCH_URL,
            params={"q": "hybrid", "mode": "hybrid"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "items" in body
    assert "total_count" in body
    # Must not be a server error wrapped in a 200 — items must be a list
    assert isinstance(body["items"], list)


@pytest.mark.asyncio
async def test_search_api_returns_200_without_model(
    setup_project: dict,
):
    """The search API returns 200 for all modes when no embedding model exists.

    Iterates over keyword, semantic, and hybrid modes to confirm none return 500.
    Semantic and hybrid may return empty results, but must not crash.
    """
    data = setup_project
    authed_client: AsyncClient = data["client"]

    for mode in ("keyword", "semantic", "hybrid"):
        resp = await authed_client.get(
            SEARCH_URL,
            params={"q": "test", "mode": mode},
        )
        assert resp.status_code == 200, f"mode={mode!r} returned {resp.status_code}: {resp.text}"
        body = resp.json()
        assert "items" in body, f"mode={mode!r}: response missing 'items' key"
        assert isinstance(body["items"], list), f"mode={mode!r}: 'items' is not a list"


@pytest.mark.asyncio
async def test_keyword_search_returns_correct_results_without_model(
    setup_project: dict,
):
    """Keyword search finds the expected issue even when no embedding model exists."""
    data = setup_project
    authed_client: AsyncClient = data["client"]
    project: Project = data["project"]
    tracker = data["tracker"]

    await _create_issue(
        authed_client,
        project.key,
        tracker.id,
        "Unique keyword noembeddingxyz issue",
    )

    resp = await authed_client.get(
        SEARCH_URL,
        params={"q": "noembeddingxyz", "mode": "keyword"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    issue_items = [i for i in body["items"] if i["result_type"] == "issue"]
    titles = [i["subtitle"] for i in issue_items]
    assert any("noembeddingxyz" in t for t in titles), f"Expected to find the issue in results, got: {titles}"
