"""The search backfill command and the admin backfill endpoint report what they really did.

The command rebuilds chunks for every source (all attachments, wherever they
are attached; comments under the current settings) and embeds them when the
default model can run. Without a model it writes chunks only, says so, and
exits 0 unless ``--require-model`` is given.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.cli import backfill_embeddings as cli
from specivo.core.config import get_settings
from specivo.models.attachment import Attachment
from specivo.models.search import ChunkEmbedding, EmbeddingModel, SearchChunk, SearchSource
from specivo.schemas.issue import IssueCreate
from specivo.services.issue_service import IssueService
from specivo.services.journal_service import JournalService
from specivo.services.search_index_service import defer_search_index
from specivo.services.wiki_service import WikiService
from specivo.tasks.embeddings import backfill_model_embeddings, embed_search_sources
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Commits in these tests must not try to reach a worker."""
    monkeypatch.setattr(embed_search_sources, "apply_async", lambda *_a, **_kw: None)


@pytest_asyncio.fixture
async def no_model(db_session: AsyncSession) -> None:
    await db_session.execute(update(EmbeddingModel).values(is_default=False))
    await db_session.commit()


@pytest_asyncio.fixture
async def mock_model(db_session: AsyncSession) -> EmbeddingModel:
    await db_session.execute(update(EmbeddingModel).values(is_default=False))
    model = EmbeddingModel(name="bfc-mock", provider="mock", model_name="mock-32", dimensions=32, is_default=True)
    db_session.add(model)
    await db_session.commit()
    return model


@pytest_asyncio.fixture
async def world(db_session: AsyncSession) -> SimpleNamespace:
    status = StatusFactory.build(name="New", position=1, category="backlog")
    db_session.add(status)
    await db_session.flush()
    tracker = TrackerFactory.build(name="Bug", default_status_id=status.id)
    priority = PriorityFactory.build(name="Normal", is_default=True, position=2)
    project = ProjectFactory.build(key="BFCA", name="Backfill CLI", is_public=True)
    admin = AdminUserFactory.build(login="bfc_admin", status="active")
    db_session.add_all([tracker, priority, project, admin])
    await db_session.commit()
    return SimpleNamespace(tracker=tracker, project=project, admin=admin)


@pytest_asyncio.fixture
async def content(db_session: AsyncSession, world: SimpleNamespace) -> SimpleNamespace:
    """One of each source, written before indexing ran (as on an upgraded install)."""
    defer_search_index(db_session)
    issue = await IssueService().create(
        db_session,
        world.project,
        IssueCreate(
            project_key=world.project.key,
            tracker_id=world.tracker.id,
            subject="Weir inspection",
            description="Spillway gates checked",
        ),
        world.admin,
    )
    journal = await JournalService().add_comment(db_session, issue, world.admin, "Gate four needs a new seal.")
    page, _ = await WikiService().create_page(
        db_session, world.project.id, "Weir Manual", "Open gates slowly.", world.admin
    )
    await db_session.flush()
    # No description: the filename alone is indexed.
    issue_file = Attachment(
        container_type="Issue",
        container_id=issue.id,
        filename="gate-photos.zip",
        disk_filename="bfc-gate-photos-0001.zip",
        filesize=5,
        author_id=world.admin.id,
    )
    # Attached to a wiki page rather than an issue.
    wiki_file = Attachment(
        container_type="WikiPage",
        container_id=page.id,
        filename="weir-diagram.pdf",
        disk_filename="bfc-weir-diagram-0001.pdf",
        filesize=5,
        author_id=world.admin.id,
    )
    db_session.add_all([issue_file, wiki_file])
    await db_session.commit()
    db_session.info.pop("search_index_deferred")
    return SimpleNamespace(issue=issue, journal=journal, page=page, issue_file=issue_file, wiki_file=wiki_file)


def _factory(session: AsyncSession):
    """A session factory that hands out the test's rolled-back session."""

    @asynccontextmanager
    async def _session():
        yield session

    return _session


async def _chunks(db: AsyncSession, source_type: str, entity_id: int) -> list[str]:
    rows = await db.execute(
        select(SearchChunk.content)
        .join(SearchSource, SearchSource.id == SearchChunk.source_id)
        .where(SearchSource.source_type == source_type, SearchSource.entity_id == entity_id)
        .order_by(SearchChunk.chunk_index)
    )
    return list(rows.scalars())


async def _embeddings(db: AsyncSession, source_type: str, entity_id: int) -> list[list[float]]:
    rows = await db.execute(
        select(ChunkEmbedding.embedding)
        .join(SearchChunk, SearchChunk.id == ChunkEmbedding.chunk_id)
        .join(SearchSource, SearchSource.id == SearchChunk.source_id)
        .where(SearchSource.source_type == source_type, SearchSource.entity_id == entity_id)
        .order_by(SearchChunk.chunk_index)
    )
    return [list(v) for v in rows.scalars()]


def _sources(content: SimpleNamespace) -> list[tuple[str, int]]:
    return [
        ("issue", content.issue.id),
        ("journal", content.journal.id),
        ("wiki_page", content.page.id),
        ("attachment", content.issue_file.id),
        ("attachment", content.wiki_file.id),
    ]


# ---------------------------------------------------------------------------
# Counts and the no-model path
# ---------------------------------------------------------------------------


async def test_without_a_model_writes_chunks_only_and_says_so(db_session, content, no_model, caplog):
    for source_type, entity_id in _sources(content):
        assert await _chunks(db_session, source_type, entity_id) == [], source_type

    report = await cli.run_backfill(_factory(db_session), log=lambda _m: None)

    assert report.model_usable is False
    assert report.sources_chunked == 5
    assert report.chunks == 5
    assert report.chunks_embedded == 0
    assert report.failures == 0
    assert report.exit_code == cli.EXIT_OK
    for source_type, entity_id in _sources(content):
        assert await _chunks(db_session, source_type, entity_id) != [], source_type
        assert await _embeddings(db_session, source_type, entity_id) == [], source_type

    with caplog.at_level(logging.INFO, logger="specivo.cli.backfill_embeddings"):
        cli.summarize(report)
    assert "Chunked 5 sources into 5 chunks" in caplog.text
    assert "Only keyword-search chunks were written; 0 chunks were embedded" in caplog.text
    assert "semantic search stays off" in caplog.text


async def test_with_a_model_embeds_every_source_type(db_session, content, mock_model, caplog):
    report = await cli.run_backfill(_factory(db_session), log=lambda _m: None)

    assert report.model_usable is True
    assert report.model_name == "bfc-mock"
    assert (report.sources_chunked, report.chunks, report.chunks_embedded) == (5, 5, 5)
    assert report.chunks_with_embedding == report.chunks_total
    for source_type, entity_id in _sources(content):
        assert len(await _embeddings(db_session, source_type, entity_id)) == 1, source_type

    with caplog.at_level(logging.INFO, logger="specivo.cli.backfill_embeddings"):
        cli.summarize(report)
    assert "Embedded 5 chunks with bfc-mock" in caplog.text
    assert "WARNING" not in caplog.text

    # A second run finds nothing left to embed.
    again = await cli.run_backfill(_factory(db_session), log=lambda _m: None)
    assert (again.sources_chunked, again.chunks_embedded) == (5, 0)


async def test_attachments_without_description_and_on_wiki_pages_are_indexed(db_session, content, no_model):
    await cli.run_backfill(_factory(db_session), log=lambda _m: None)

    issue_file_chunks = await _chunks(db_session, "attachment", content.issue_file.id)
    wiki_file_chunks = await _chunks(db_session, "attachment", content.wiki_file.id)
    assert len(issue_file_chunks) == 1 and "gate" in issue_file_chunks[0]
    assert len(wiki_file_chunks) == 1 and "diagram" in wiki_file_chunks[0]
    project_ids = set(
        (
            await db_session.execute(
                select(SearchSource.project_id).where(
                    SearchSource.source_type == "attachment",
                    SearchSource.entity_id.in_([content.issue_file.id, content.wiki_file.id]),
                )
            )
        ).scalars()
    )
    assert project_ids == {content.issue.project_id}


async def test_removes_chunks_of_comments_the_settings_now_exclude(db_session, content, no_model, monkeypatch):
    await cli.run_backfill(_factory(db_session), log=lambda _m: None)
    assert await _chunks(db_session, "journal", content.journal.id) != []

    monkeypatch.setattr(get_settings(), "search_index_comments", False)
    report = await cli.run_backfill(_factory(db_session), log=lambda _m: None)

    assert await _chunks(db_session, "journal", content.journal.id) == []
    assert report.sources_removed == 1
    assert report.sources_chunked == 4


async def test_removes_sources_whose_item_is_gone(db_session, content, no_model):
    await cli.run_backfill(_factory(db_session), log=lambda _m: None)
    # A bulk delete bypasses the session events, leaving a stale source behind.
    await db_session.execute(Attachment.__table__.delete().where(Attachment.id == content.wiki_file.id))
    await db_session.commit()
    assert await _chunks(db_session, "attachment", content.wiki_file.id) != []

    report = await cli.run_backfill(_factory(db_session), log=lambda _m: None)
    assert await _chunks(db_session, "attachment", content.wiki_file.id) == []
    assert report.sources_removed == 1


async def test_reembed_recomputes_existing_embeddings(db_session, content, mock_model):
    await cli.run_backfill(_factory(db_session), log=lambda _m: None)
    original = await _embeddings(db_session, "issue", content.issue.id)
    stale = [0.0] * 31 + [1.0]
    chunk_ids = (
        select(SearchChunk.id)
        .join(SearchSource)
        .where(SearchSource.source_type == "issue", SearchSource.entity_id == content.issue.id)
    )
    await db_session.execute(
        update(ChunkEmbedding).where(ChunkEmbedding.chunk_id.in_(chunk_ids)).values(embedding=stale)
    )
    await db_session.commit()

    plain = await cli.run_backfill(_factory(db_session), log=lambda _m: None)
    assert plain.chunks_embedded == 0
    assert await _embeddings(db_session, "issue", content.issue.id) == [pytest.approx(stale)]

    report = await cli.run_backfill(_factory(db_session), reembed=True, log=lambda _m: None)
    assert report.chunks_embedded == 5
    assert await _embeddings(db_session, "issue", content.issue.id) == [pytest.approx(v) for v in original]
    total = (await db_session.execute(select(func.count()).select_from(ChunkEmbedding))).scalar_one()
    assert report.chunks_with_embedding == total


async def test_a_failing_source_is_reported_and_sets_exit_1(db_session, content, no_model, monkeypatch):
    from specivo.services import search_index_service

    real = search_index_service.load_source_chunks

    def _flaky(conn, source_type, entity_id):
        if source_type == "issue" and entity_id == content.issue.id:
            raise RuntimeError("chunker exploded")
        return real(conn, source_type, entity_id)

    monkeypatch.setattr(search_index_service, "load_source_chunks", _flaky)
    report = await cli.run_backfill(_factory(db_session), log=lambda _m: None)

    assert report.failures == 1
    assert report.sources_chunked == 4
    assert report.exit_code == cli.EXIT_ERRORS


class TestMain:
    @pytest.fixture(autouse=True)
    def _use_test_session(self, db_session, monkeypatch):
        monkeypatch.setattr("specivo.core.database.get_session_factory", lambda: _factory(db_session))

    async def test_require_model_fails_without_a_model_and_does_nothing(self, db_session, content, no_model, caplog):
        with caplog.at_level(logging.ERROR, logger="specivo.cli.backfill_embeddings"):
            code = await cli._main_async(reembed=False, require_model=True)
        assert code == cli.EXIT_NO_MODEL
        assert "--require-model" in caplog.text
        assert await _chunks(db_session, "issue", content.issue.id) == []

    async def test_no_model_without_the_flag_exits_0(self, db_session, content, no_model):
        assert await cli._main_async(reembed=False, require_model=False) == cli.EXIT_OK
        assert await _chunks(db_session, "issue", content.issue.id) != []

    async def test_require_model_with_a_model_runs(self, db_session, content, mock_model):
        assert await cli._main_async(reembed=True, require_model=True) == cli.EXIT_OK
        assert len(await _embeddings(db_session, "issue", content.issue.id)) == 1


async def test_cli_help_lists_the_options(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--reembed" in out and "--require-model" in out


# ---------------------------------------------------------------------------
# Admin endpoint
# ---------------------------------------------------------------------------

ADMIN_URL = "/api/v1/admin/embedding-models/{id}/backfill/"


class TestAdminBackfillEndpoint:
    @pytest.fixture
    def delayed(self, monkeypatch) -> list[tuple]:
        calls: list[tuple] = []
        monkeypatch.setattr(backfill_model_embeddings, "delay", lambda *args: calls.append(args))
        return calls

    async def test_fill_gaps_by_default(self, admin_client: AsyncClient, mock_model, delayed):
        resp = await admin_client.post(ADMIN_URL.format(id=mock_model.id))
        assert resp.status_code == 202, resp.text
        assert resp.json() == {"status": "backfill_started", "model_id": mock_model.id, "reembed": False}
        assert delayed == [(mock_model.id, False)]

    async def test_reembed_option(self, admin_client: AsyncClient, mock_model, delayed):
        resp = await admin_client.post(ADMIN_URL.format(id=mock_model.id), params={"reembed": "true"})
        assert resp.status_code == 202, resp.text
        assert resp.json()["reembed"] is True
        assert delayed == [(mock_model.id, True)]

    async def test_model_that_cannot_run_is_refused(self, admin_client: AsyncClient, db_session, delayed):
        model = EmbeddingModel(
            name="bfc-missing", provider="local", model_name="bfc-not-downloaded", dimensions=8, is_default=False
        )
        db_session.add(model)
        await db_session.commit()

        resp = await admin_client.post(ADMIN_URL.format(id=model.id))
        assert resp.status_code == 409, resp.text
        assert "embedding_model_unavailable" in resp.text
        assert delayed == []

    async def test_unknown_model_is_404(self, admin_client: AsyncClient, delayed):
        resp = await admin_client.post(ADMIN_URL.format(id=987654321))
        assert resp.status_code == 404
        assert delayed == []

    async def test_non_admin_is_refused(self, auth_client: AsyncClient, mock_model, delayed):
        resp = await auth_client.post(ADMIN_URL.format(id=mock_model.id), params={"reembed": "true"})
        assert resp.status_code == 403
        assert delayed == []


class TestBackfillModelService:
    async def test_reembed_replaces_vectors_in_place(self, db_session, content, mock_model):
        from specivo.services.embedding_service import EmbeddingService

        await cli.run_backfill(_factory(db_session), log=lambda _m: None)
        await db_session.execute(update(ChunkEmbedding).values(embedding=[0.0] * 31 + [1.0]))
        await db_session.commit()
        before = (await db_session.execute(select(func.count()).select_from(ChunkEmbedding))).scalar_one()

        service = EmbeddingService()
        assert await service.backfill_model(db_session, mock_model.id) == 0
        rewritten = await service.backfill_model(db_session, mock_model.id, reembed=True)

        assert rewritten == before
        after = (await db_session.execute(select(func.count()).select_from(ChunkEmbedding))).scalar_one()
        assert after == before
        issue_vector = (await _embeddings(db_session, "issue", content.issue.id))[0]
        assert issue_vector != pytest.approx([0.0] * 31 + [1.0])

    async def test_model_that_cannot_run_embeds_nothing(self, db_session, content, caplog):
        from specivo.services.embedding_service import EmbeddingService

        model = EmbeddingModel(name="bfc-absent", provider="local", model_name="bfc-absent", dimensions=8)
        db_session.add(model)
        await db_session.commit()
        await cli.run_backfill(_factory(db_session), log=lambda _m: None)

        with caplog.at_level(logging.WARNING, logger="specivo.services.embedding_service"):
            assert await EmbeddingService().backfill_model(db_session, model.id) == 0
        assert "cannot run here" in caplog.text
