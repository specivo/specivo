"""Only the bundled local embedding model can be configured, and a bad model row never breaks search.

- The admin API cannot register a model, so no hosted provider can be set up.
- The database refuses a second default model.
- A default row naming a provider that cannot run (left by an older release)
  turns semantic search off with one warning, and search falls back to keyword
  matching instead of answering 500.
- Embedding failures are logged by error class only, never with their message,
  which can quote content.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.role import Role
from specivo.models.search import EmbeddingModel
from specivo.services import embedding_service, search_service
from specivo.services.embedding_service import EmbeddingService
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import TEST_PASSWORD, UserFactory

pytestmark = pytest.mark.integration

ADMIN_MODELS_URL = "/api/v1/admin/embedding-models/"
SEARCH_URL = "/api/v1/search/"
SECRET = "confidential launch plan"


async def _only_default(db: AsyncSession, **fields: object) -> EmbeddingModel:
    """Make a model with *fields* the only default."""
    await db.execute(update(EmbeddingModel).where(EmbeddingModel.is_default.is_(True)).values(is_default=False))
    model = EmbeddingModel(is_default=True, **fields)
    db.add(model)
    await db.commit()
    await db.refresh(model)
    return model


@pytest.fixture(autouse=True)
def _fresh_warning_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """The once-per-process warnings must fire again in each test."""
    monkeypatch.setattr(embedding_service, "_warned_unsupported", set())
    monkeypatch.setattr(search_service, "_failed_embedding_models", set())


# ---------------------------------------------------------------------------
# Admin API and the single-default constraint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_api_cannot_register_a_hosted_model(admin_client: AsyncClient, db_session: AsyncSession):
    resp = await admin_client.post(
        ADMIN_MODELS_URL,
        json={
            "name": "hosted-model",
            "provider": "openai",
            "model_name": "text-embedding-3-small",
            "dimensions": 1536,
            "is_default": True,
            "api_key_encrypted": "sk-test",
        },
    )
    assert resp.status_code == 405, resp.text
    found = await db_session.execute(select(EmbeddingModel).where(EmbeddingModel.name == "hosted-model"))
    assert found.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_admin_api_cannot_register_a_second_default(admin_client: AsyncClient, db_session: AsyncSession):
    await _only_default(
        db_session, name="prov-local", provider="local", model_name="multilingual-e5-small", dimensions=384
    )
    resp = await admin_client.post(
        ADMIN_MODELS_URL,
        json={
            "name": "prov-local-2",
            "provider": "local",
            "model_name": "multilingual-e5-small",
            "dimensions": 384,
            "is_default": True,
        },
    )
    assert resp.status_code == 405, resp.text
    defaults = await db_session.execute(select(EmbeddingModel.name).where(EmbeddingModel.is_default.is_(True)))
    assert defaults.scalars().all() == ["prov-local"]


@pytest.mark.asyncio
async def test_list_still_works(admin_client: AsyncClient, db_session: AsyncSession):
    await _only_default(
        db_session, name="prov-list", provider="local", model_name="multilingual-e5-small", dimensions=384
    )
    resp = await admin_client.get(ADMIN_MODELS_URL)
    assert resp.status_code == 200, resp.text
    assert "prov-list" in [m["name"] for m in resp.json()]
    assert all("api_key_encrypted" not in m for m in resp.json())


@pytest.mark.asyncio
async def test_database_refuses_a_second_default(db_session: AsyncSession):
    await _only_default(db_session, name="prov-first", provider="mock", model_name="mock-8", dimensions=8)
    db_session.add(
        EmbeddingModel(name="prov-second", provider="mock", model_name="mock-8", dimensions=8, is_default=True)
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_a_non_default_model_can_coexist(db_session: AsyncSession):
    await _only_default(db_session, name="prov-default", provider="mock", model_name="mock-8", dimensions=8)
    db_session.add(EmbeddingModel(name="prov-other", provider="mock", model_name="mock-8", dimensions=8))
    await db_session.flush()


# ---------------------------------------------------------------------------
# A default row whose provider cannot run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unsupported_provider_embeds_nothing_and_warns_once(
    db_session: AsyncSession, caplog: pytest.LogCaptureFixture
):
    model = await _only_default(
        db_session, name="prov-hosted", provider="openai", model_name="text-embedding-3-small", dimensions=1536
    )
    service = EmbeddingService()
    assert not service.is_model_usable(model)
    with caplog.at_level(logging.WARNING, logger="specivo.services.embedding_service"):
        assert await service.generate_embedding("first", model, intent="query") is None
        assert await service.generate_embedding("second", model) is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "not supported" in warnings[0].getMessage()
    assert await service.embed_pending(db_session, [("issue", 1)]) == 0


@pytest_asyncio.fixture
async def search_client(db_session: AsyncSession, client: AsyncClient) -> tuple[AsyncClient, Project, int]:
    status = StatusFactory.build(name="New", position=1, category="backlog")
    db_session.add(status)
    await db_session.flush()
    tracker = TrackerFactory.build(name="Bug", default_status_id=status.id)
    db_session.add(tracker)
    db_session.add(PriorityFactory.build(name="Normal", is_default=True, position=2))
    project = ProjectFactory.build(key="PRVD", identifier="provider-degradation")
    user = UserFactory.build(login="provider_search_user", status="active")
    db_session.add_all([project, user])
    await db_session.flush()
    role = Role(name="Mgr-PRVD", permissions=["*"], builtin=0)
    db_session.add(role)
    await db_session.flush()
    member = Member(user_id=user.id, project_id=project.id)
    db_session.add(member)
    await db_session.flush()
    db_session.add(MemberRole(member_id=member.id, role_id=role.id))
    await db_session.commit()

    resp = await client.post("/api/v1/auth/login/", json={"login": user.login, "password": TEST_PASSWORD})
    assert resp.status_code == 200, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['access_token']}"
    return client, project, tracker.id


async def _create_issue(client: AsyncClient, project: Project, tracker_id: int, subject: str) -> None:
    resp = await client.post(
        f"/api/v1/projects/{project.key}/issues/",
        json={"project_key": project.key, "tracker_id": tracker_id, "subject": subject},
    )
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_search_falls_back_to_keyword_with_a_hosted_default_model(
    search_client: tuple[AsyncClient, Project, int],
    db_session: AsyncSession,
    caplog: pytest.LogCaptureFixture,
):
    client, project, tracker_id = search_client
    await _only_default(
        db_session, name="prov-hosted", provider="openai", model_name="text-embedding-3-small", dimensions=1536
    )
    await _create_issue(client, project, tracker_id, "Quarterly zephyrine budget review")

    with caplog.at_level(logging.WARNING):
        for mode in ("hybrid", "semantic", "keyword"):
            resp = await client.get(SEARCH_URL, params={"q": "zephyrine", "mode": mode})
            assert resp.status_code == 200, f"{mode}: {resp.text}"
            titles = [item.get("title") for item in resp.json()["items"]]
            if mode != "semantic":
                assert "PRVD-1" in titles, (mode, titles)

    unsupported = [r for r in caplog.records if "not supported" in r.getMessage()]
    assert len(unsupported) == 1


@pytest.mark.asyncio
async def test_search_falls_back_to_keyword_when_query_embedding_raises(
    search_client: tuple[AsyncClient, Project, int],
    db_session: AsyncSession,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
):
    client, project, tracker_id = search_client
    await _only_default(db_session, name="prov-broken", provider="mock", model_name="mock-8", dimensions=8)
    await _create_issue(client, project, tracker_id, "Quarterly zephyrine budget review")

    async def _boom(*_args: object, **_kwargs: object) -> list[float]:
        raise RuntimeError(f"tokenizer choked on {SECRET}")

    monkeypatch.setattr(EmbeddingService, "generate_embedding", _boom)

    with caplog.at_level(logging.WARNING, logger="specivo.services.search_service"):
        for _ in range(2):
            resp = await client.get(SEARCH_URL, params={"q": "zephyrine", "mode": "hybrid"})
            assert resp.status_code == 200, resp.text
            assert "PRVD-1" in [i.get("title") for i in resp.json()["items"]]
        resp = await client.get(SEARCH_URL, params={"q": "zephyrine", "mode": "semantic"})
        assert resp.status_code == 200, resp.text

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "RuntimeError" in warnings[0].getMessage()
    assert SECRET not in caplog.text


# ---------------------------------------------------------------------------
# Embedding tasks log the error class, not its message
# ---------------------------------------------------------------------------


def test_backfill_task_logs_no_exception_text(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture):
    from specivo.tasks import embeddings

    def _fail(coro: object) -> None:
        coro.close()  # type: ignore[attr-defined]
        raise RuntimeError(f"duplicate key for chunk text {SECRET}")

    monkeypatch.setattr(embeddings, "run_async", _fail)
    with caplog.at_level(logging.DEBUG, logger="specivo.tasks.embeddings"):
        with pytest.raises(RuntimeError) as raised:
            embeddings.backfill_model_embeddings.run(7)

    assert SECRET not in str(raised.value)
    assert "RuntimeError" in str(raised.value)
    warning_text = " ".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
    assert "Failed to backfill model 7: RuntimeError" in warning_text
    assert SECRET not in warning_text


def test_embed_task_logs_no_exception_text(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture):
    from specivo.tasks import embeddings

    def _fail(coro: object) -> None:
        coro.close()  # type: ignore[attr-defined]
        raise RuntimeError(f"value too long for {SECRET}")

    monkeypatch.setattr(embeddings, "run_async", _fail)
    with caplog.at_level(logging.WARNING, logger="specivo.tasks.embeddings"):
        with pytest.raises(RuntimeError) as raised:
            embeddings.embed_search_sources.run([["issue", 1]])

    assert SECRET not in str(raised.value)
    assert "RuntimeError" in caplog.text
    assert SECRET not in caplog.text


@pytest.mark.asyncio
async def test_embed_task_per_source_failure_logs_no_exception_text(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    from specivo.tasks import embeddings

    session = MagicMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()

    @asynccontextmanager
    async def _session():  # type: ignore[no-untyped-def]
        yield session

    model = EmbeddingModel(id=1, name="prov-mock", provider="mock", model_name="mock-8", dimensions=8)
    monkeypatch.setattr(embeddings, "task_session", _session)
    monkeypatch.setattr(EmbeddingService, "get_default_model", AsyncMock(return_value=model))
    monkeypatch.setattr(EmbeddingService, "embed_pending", AsyncMock(side_effect=RuntimeError(f"bad row {SECRET}")))

    with caplog.at_level(logging.WARNING, logger="specivo.tasks.embeddings"):
        await embeddings._embed_sources_async([("issue", 1), ("wiki_page", 2)])

    assert caplog.text.count("Embedding failed for") == 2
    assert "RuntimeError" in caplog.text
    assert SECRET not in caplog.text
    session.rollback.assert_awaited()
