"""Search chunks follow content as it is written, with or without an embedding model.

Covers the session events in ``search_index_service``: chunks are written,
replaced and deleted in the writing transaction for issues, wiki pages,
comments and attachments, so keyword search sees changes at once; embedding is
queued once per source after commit and run by ``embed_search_sources``.
"""

from __future__ import annotations

import importlib.util
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.models.attachment import Attachment
from specivo.models.issue import Issue
from specivo.models.journal import Journal
from specivo.models.member import Member, MemberRole
from specivo.models.role import Role
from specivo.models.search import ChunkEmbedding, EmbeddingModel, SearchChunk, SearchSource
from specivo.models.user import User
from specivo.schemas.issue import IssueCreate, IssueUpdate
from specivo.services import search_index_service
from specivo.services.attachment_service import AttachmentService
from specivo.services.issue_service import IssueService
from specivo.services.journal_service import JournalService
from specivo.services.search_index_service import defer_search_index
from specivo.services.search_service import SearchService
from specivo.services.wiki_service import WikiService
from specivo.tasks.embeddings import embed_search_sources
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, ServiceAccountFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

_search = SearchService()


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def queued(monkeypatch: pytest.MonkeyPatch) -> list[list[list]]:
    """Record each embedding task the commits queue, one entry per task."""
    calls: list[list[list]] = []
    monkeypatch.setattr(embed_search_sources, "apply_async", lambda args, **_kw: calls.append(args[0]))
    return calls


@pytest_asyncio.fixture
async def no_model(db_session: AsyncSession) -> None:
    """No default embedding model: the install has keyword search only."""
    await db_session.execute(update(EmbeddingModel).values(is_default=False))
    await db_session.commit()


@pytest_asyncio.fixture
async def mock_model(db_session: AsyncSession) -> EmbeddingModel:
    """A default mock model, which can always embed."""
    await db_session.execute(update(EmbeddingModel).values(is_default=False))
    model = EmbeddingModel(name="six-mock", provider="mock", model_name="mock-64", dimensions=64, is_default=True)
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
    project = ProjectFactory.build(key="SIXA", name="Index Sync A", is_public=True)
    other = ProjectFactory.build(key="SIXB", name="Index Sync B", is_public=True)
    admin = AdminUserFactory.build(login="six_admin", status="active")
    reader = UserFactory.build(login="six_reader", status="active")
    bot = ServiceAccountFactory.build(login="six_bot", status="active")
    db_session.add_all([tracker, priority, project, other, admin, reader, bot])
    await db_session.flush()

    role = Role(name="SIX Reader", permissions=["view_issues", "view_wiki"], builtin=0, issues_visibility="all")
    db_session.add(role)
    await db_session.flush()
    member = Member(user_id=reader.id, project_id=project.id)
    db_session.add(member)
    await db_session.flush()
    db_session.add(MemberRole(member_id=member.id, role_id=role.id))
    await db_session.commit()
    return SimpleNamespace(
        tracker=tracker,
        status=status,
        priority=priority,
        project=project,
        other=other,
        admin=admin,
        reader=reader,
        bot=bot,
    )


async def _new_issue(db: AsyncSession, world: SimpleNamespace, subject: str, description: str | None = None) -> Issue:
    issue = await IssueService().create(
        db,
        world.project,
        IssueCreate(
            project_key=world.project.key, tracker_id=world.tracker.id, subject=subject, description=description
        ),
        world.admin,
    )
    await db.commit()
    return issue


async def _found(db: AsyncSession, user: User, query: str, scope: str, **kwargs) -> set[int]:
    results, _total, _counts = await _search.search(db, query, user, scope=scope, **kwargs)
    return {r.id for r in results}


async def _chunks(db: AsyncSession, source_type: str, entity_id: int) -> list[str]:
    rows = await db.execute(
        select(SearchChunk.content)
        .join(SearchSource, SearchSource.id == SearchChunk.source_id)
        .where(SearchSource.source_type == source_type, SearchSource.entity_id == entity_id)
        .order_by(SearchChunk.chunk_index)
    )
    return list(rows.scalars())


async def _source_project(db: AsyncSession, source_type: str, entity_id: int) -> int | None:
    stmt = select(SearchSource.project_id).where(
        SearchSource.source_type == source_type, SearchSource.entity_id == entity_id
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def _embedding_count(db: AsyncSession, source_type: str, entity_id: int) -> int:
    stmt = (
        select(func.count())
        .select_from(ChunkEmbedding)
        .join(SearchChunk, SearchChunk.id == ChunkEmbedding.chunk_id)
        .join(SearchSource, SearchSource.id == SearchChunk.source_id)
        .where(SearchSource.source_type == source_type, SearchSource.entity_id == entity_id)
    )
    return (await db.execute(stmt)).scalar_one()


# ---------------------------------------------------------------------------
# Keyword search follows every change, with no model installed
# ---------------------------------------------------------------------------


class TestKeywordWithoutModel:
    async def test_issue_created_edited_and_deleted(self, db_session, world, no_model, queued):
        issue = await _new_issue(db_session, world, "Pelican migration report", "Spring counts along the coast")
        assert await _chunks(db_session, "issue", issue.id) == [
            "Pelican migration report\n\nSpring counts along the coast"
        ]
        assert issue.id in await _found(db_session, world.admin, "pelican", "issues")

        await IssueService().update(
            db_session,
            issue,
            IssueUpdate(lock_version=issue.lock_version, description="Autumn counts inland"),
            world.admin,
        )
        await db_session.commit()
        assert await _chunks(db_session, "issue", issue.id) == ["Pelican migration report\n\nAutumn counts inland"]
        assert issue.id in await _found(db_session, world.admin, "autumn", "issues")
        assert issue.id not in await _found(db_session, world.admin, "spring", "issues")

        issue_id = issue.id
        await IssueService().delete(db_session, issue)
        await db_session.commit()
        assert await _chunks(db_session, "issue", issue_id) == []
        assert issue_id not in await _found(db_session, world.admin, "pelican", "issues")
        # No model, so nothing was ever queued for embedding.
        assert queued == []

    async def test_wiki_page_created_edited_and_deleted(self, db_session, world, no_model):
        wiki = WikiService()
        page, _ = await wiki.create_page(
            db_session, world.project.id, "Lighthouse Rota", "Keepers rotate weekly.", world.admin
        )
        await db_session.commit()
        assert await _chunks(db_session, "wiki_page", page.id) == ["Lighthouse Rota\n\nKeepers rotate weekly."]
        assert page.id in await _found(db_session, world.admin, "keepers", "wiki")

        page, _ = await wiki.update_page(db_session, page.id, "Keepers rotate monthly.", world.admin, page.lock_version)
        await db_session.commit()
        assert await _chunks(db_session, "wiki_page", page.id) == ["Lighthouse Rota\n\nKeepers rotate monthly."]
        assert page.id in await _found(db_session, world.admin, "monthly", "wiki")
        assert page.id not in await _found(db_session, world.admin, "weekly", "wiki")

        await wiki.delete_page(db_session, page.id, world.admin)
        await db_session.commit()
        assert await _chunks(db_session, "wiki_page", page.id) == []
        assert page.id not in await _found(db_session, world.admin, "keepers", "wiki")

        await wiki.restore_page(db_session, page.id)
        await db_session.commit()
        assert await _chunks(db_session, "wiki_page", page.id) == ["Lighthouse Rota\n\nKeepers rotate monthly."]

    async def test_wiki_children_deleted_in_bulk_lose_their_chunks(self, db_session, world, no_model):
        wiki = WikiService()
        parent, _ = await wiki.create_page(db_session, world.project.id, "Harbour", "Harbour overview.", world.admin)
        child, _ = await wiki.create_page(
            db_session, world.project.id, "Moorings", "Mooring buoys list.", world.admin, parent_slug=parent.slug
        )
        await db_session.commit()
        assert await _chunks(db_session, "wiki_page", child.id) != []

        await wiki.delete_page(db_session, parent.id, world.admin, cascade_children=True)
        await db_session.commit()
        assert await _chunks(db_session, "wiki_page", child.id) == []

    async def test_comment_created_edited_and_deleted(self, db_session, world, no_model):
        issue = await _new_issue(db_session, world, "Ferry timetable")
        journal = await JournalService().add_comment(
            db_session, issue, world.admin, "The evening ferry now leaves at seven."
        )
        await db_session.commit()
        assert journal.id in await _found(db_session, world.admin, "evening ferry", "comments")

        journal.notes = "The morning ferry now leaves at six."
        await db_session.commit()
        assert journal.id in await _found(db_session, world.admin, "morning", "comments")
        assert journal.id not in await _found(db_session, world.admin, "evening", "comments")

        await db_session.delete(journal)
        await db_session.commit()
        assert await _chunks(db_session, "journal", journal.id) == []
        assert journal.id not in await _found(db_session, world.admin, "ferry", "comments")

    async def test_comment_posted_through_the_api_is_found(self, admin_client: AsyncClient, db_session, no_model):
        status = StatusFactory.build(name="New", position=1, category="backlog")
        db_session.add(status)
        await db_session.flush()
        tracker = TrackerFactory.build(name="Bug", default_status_id=status.id)
        db_session.add_all([tracker, PriorityFactory.build(name="Normal", is_default=True, position=2)])
        project = ProjectFactory.build(key="SIXAPI", is_public=True)
        db_session.add(project)
        await db_session.commit()

        resp = await admin_client.post(
            "/api/v1/projects/SIXAPI/issues/",
            json={"project_key": "SIXAPI", "tracker_id": tracker.id, "subject": "Bridge"},
        )
        assert resp.status_code == 201, resp.text
        key = resp.json()["key"]
        resp = await admin_client.post(
            f"/api/v1/issues/{key}/journals/", json={"notes": "Drawbridge repainted in teal."}
        )
        assert resp.status_code == 201, resp.text

        resp = await admin_client.get("/api/v1/search/", params={"q": "drawbridge teal", "scope": "comments"})
        assert resp.status_code == 200, resp.text
        assert [r["result_type"] for r in resp.json()["items"]] == ["comment"]

    async def test_attachment_created_edited_and_deleted(self, db_session, world, no_model):
        issue = await _new_issue(db_session, world, "Survey photos")
        attachment = Attachment(
            container_type="Issue",
            container_id=issue.id,
            filename="dunes.txt",
            disk_filename="six-dunes-0001.txt",
            content_type="text/plain",
            filesize=10,
            author_id=world.admin.id,
            description="Sand dune transect",
        )
        db_session.add(attachment)
        await db_session.commit()
        assert attachment.id in await _found(db_session, world.admin, "transect", "attachments")

        await AttachmentService().update_description(db_session, attachment, "Salt marsh transect", world.admin)
        await db_session.commit()
        assert attachment.id in await _found(db_session, world.admin, "marsh", "attachments")
        assert attachment.id not in await _found(db_session, world.admin, "sand", "attachments")

        attachment.metadata = {"extracted_text": "Heron nesting sites were mapped."}
        await db_session.commit()
        assert attachment.id in await _found(db_session, world.admin, "heron", "attachments")

        attachment_id = attachment.id
        await AttachmentService().delete(db_session, attachment, world.admin)
        await db_session.commit()
        assert await _chunks(db_session, "attachment", attachment_id) == []


# ---------------------------------------------------------------------------
# Comment visibility and settings
# ---------------------------------------------------------------------------


class TestCommentRules:
    async def test_private_note_hidden_from_reader(self, db_session, world, no_model):
        issue = await _new_issue(db_session, world, "Budget review")
        public = await JournalService().add_comment(db_session, issue, world.admin, "Quarterly kayak budget approved.")
        private = Journal(
            issue_id=issue.id,
            project_id=issue.project_id,
            user_id=world.admin.id,
            notes="Private kayak supplier quote attached.",
            is_private=True,
            sequence=99,
        )
        db_session.add(private)
        await db_session.commit()

        assert await _found(db_session, world.admin, "kayak", "comments") == {public.id, private.id}
        assert await _found(db_session, world.reader, "kayak", "comments") == {public.id}

        # Making a note private hides it at once; search reads the flag live.
        public.is_private = True
        await db_session.commit()
        assert await _found(db_session, world.reader, "kayak", "comments") == set()

    async def test_short_comments_are_not_indexed(self, db_session, world, no_model, monkeypatch):
        monkeypatch.setattr(get_settings(), "search_min_comment_length", 30)
        issue = await _new_issue(db_session, world, "Length rule")
        short = await JournalService().add_comment(db_session, issue, world.admin, "Looks fine to me.")
        long = await JournalService().add_comment(db_session, issue, world.admin, "Looks fine to me after the retest.")
        await db_session.commit()
        assert await _chunks(db_session, "journal", short.id) == []
        assert await _chunks(db_session, "journal", long.id) != []

        # Shortening an indexed comment below the limit drops its chunk.
        long.notes = "Fine."
        await db_session.commit()
        assert await _chunks(db_session, "journal", long.id) == []

    async def test_bot_comments_follow_the_setting(self, db_session, world, no_model, monkeypatch):
        issue = await _new_issue(db_session, world, "Bot rule")
        excluded = await JournalService().add_comment(db_session, issue, world.bot, "Automated build passed on main.")
        await db_session.commit()
        assert await _chunks(db_session, "journal", excluded.id) == []

        monkeypatch.setattr(get_settings(), "search_exclude_bot_comments", False)
        included = await JournalService().add_comment(db_session, issue, world.bot, "Automated build passed again.")
        await db_session.commit()
        assert await _chunks(db_session, "journal", included.id) == ["Automated build passed again."]

    async def test_comment_indexing_can_be_switched_off(self, db_session, world, no_model, monkeypatch):
        monkeypatch.setattr(get_settings(), "search_index_comments", False)
        issue = await _new_issue(db_session, world, "Switch rule")
        journal = await JournalService().add_comment(db_session, issue, world.admin, "This comment is long enough.")
        await db_session.commit()
        assert await _chunks(db_session, "journal", journal.id) == []
        # Issues are still indexed.
        assert await _chunks(db_session, "issue", issue.id) == ["Switch rule"]


# ---------------------------------------------------------------------------
# Transactions and embedding dispatch
# ---------------------------------------------------------------------------


class TestDispatch:
    async def test_rollback_leaves_no_chunks_and_queues_nothing(self, db_session, world, mock_model, queued):
        issue = await IssueService().create(
            db_session,
            world.project,
            IssueCreate(project_key=world.project.key, tracker_id=world.tracker.id, subject="Abandoned draft"),
            world.admin,
        )
        issue_id = issue.id
        assert await _chunks(db_session, "issue", issue_id) == ["Abandoned draft"]

        await db_session.rollback()
        assert await _chunks(db_session, "issue", issue_id) == []
        await db_session.commit()
        assert queued == []

    async def test_one_task_per_commit_and_each_source_once(self, db_session, world, mock_model, queued):
        issue = await IssueService().create(
            db_session,
            world.project,
            IssueCreate(project_key=world.project.key, tracker_id=world.tracker.id, subject="Tide gauge"),
            world.admin,
        )
        for text in ("Reading one", "Reading two"):
            issue.description = text
            await db_session.flush()
        journal = await JournalService().add_comment(db_session, issue, world.admin, "Gauge recalibrated this week.")
        await db_session.commit()

        assert queued == [[["issue", issue.id], ["journal", journal.id]]]

    async def test_unchanged_text_queues_nothing(self, db_session, world, mock_model, queued):
        issue = await _new_issue(db_session, world, "Steady text")
        queued.clear()
        issue.subject = "Steady text"
        issue.done_ratio = 50
        await db_session.commit()
        assert queued == []

    async def test_broker_failure_does_not_fail_the_request(
        self, admin_client: AsyncClient, db_session, mock_model, monkeypatch, caplog
    ):
        def _down(*_args, **_kwargs):
            raise ConnectionError("broker unreachable")

        monkeypatch.setattr(embed_search_sources, "apply_async", _down)
        status = StatusFactory.build(name="New", position=1, category="backlog")
        db_session.add(status)
        await db_session.flush()
        tracker = TrackerFactory.build(name="Bug", default_status_id=status.id)
        db_session.add_all([tracker, PriorityFactory.build(name="Normal", is_default=True, position=2)])
        db_session.add(ProjectFactory.build(key="SIXBRK", is_public=True))
        await db_session.commit()

        with caplog.at_level(logging.WARNING, logger="specivo.services.search_index_service"):
            resp = await admin_client.post(
                "/api/v1/projects/SIXBRK/issues/",
                json={"project_key": "SIXBRK", "tracker_id": tracker.id, "subject": "Breakwater"},
            )
        assert resp.status_code == 201, resp.text
        assert "Could not queue embedding" in caplog.text
        # The chunk is in place for keyword search and a later backfill.
        assert await _chunks(db_session, "issue", resp.json()["id"]) == ["Breakwater"]

    async def test_indexing_failure_is_logged_and_does_not_fail_the_write(
        self, db_session, world, no_model, monkeypatch, caplog
    ):
        def _boom(*_args, **_kwargs):
            raise RuntimeError("chunker exploded")

        monkeypatch.setattr(search_index_service, "load_source_chunks", _boom)
        with caplog.at_level(logging.WARNING, logger="specivo.services.search_index_service"):
            issue = await _new_issue(db_session, world, "Still saved")
        assert issue.id is not None
        assert f"Search indexing failed for issue {issue.id}" in caplog.text
        assert await _chunks(db_session, "issue", issue.id) == []


class TestEmbeddingTask:
    @staticmethod
    def _use_session(monkeypatch, session: AsyncSession) -> None:
        @asynccontextmanager
        async def _session():
            yield session

        monkeypatch.setattr("specivo.tasks.embeddings.task_session", _session)

    async def test_task_embeds_pending_chunks(self, db_session, world, mock_model, queued, monkeypatch):
        issue = await _new_issue(db_session, world, "Reef survey", "Coral cover estimates")
        assert await _embedding_count(db_session, "issue", issue.id) == 0
        self._use_session(monkeypatch, db_session)

        from specivo.tasks.embeddings import _embed_sources_async

        await _embed_sources_async([("issue", issue.id)])
        assert await _embedding_count(db_session, "issue", issue.id) == 1

        # Idempotent: a second run, or a run for a vanished source, adds nothing.
        await _embed_sources_async([("issue", issue.id), ("issue", 987654321)])
        assert await _embedding_count(db_session, "issue", issue.id) == 1

    async def test_edit_drops_stale_embeddings(self, db_session, world, mock_model, queued, monkeypatch):
        from specivo.tasks.embeddings import _embed_sources_async

        self._use_session(monkeypatch, db_session)
        issue = await _new_issue(db_session, world, "Kelp forest")
        await _embed_sources_async([("issue", issue.id)])
        assert await _embedding_count(db_session, "issue", issue.id) == 1

        issue.description = "Urchin barrens spreading"
        await db_session.commit()
        assert await _embedding_count(db_session, "issue", issue.id) == 0
        assert queued[-1] == [["issue", issue.id]]

    async def test_task_is_a_no_op_without_a_model(self, db_session, world, no_model, monkeypatch):
        from specivo.tasks.embeddings import _embed_sources_async

        self._use_session(monkeypatch, db_session)
        issue = await _new_issue(db_session, world, "Mangrove plots")
        await _embed_sources_async([("issue", issue.id)])
        assert await _embedding_count(db_session, "issue", issue.id) == 0
        assert await _chunks(db_session, "issue", issue.id) == ["Mangrove plots"]


# ---------------------------------------------------------------------------
# Issue move
# ---------------------------------------------------------------------------


async def test_move_rescopes_issue_comment_and_attachment_chunks(db_session, world, no_model):
    issue = await _new_issue(db_session, world, "Seagrass mapping")
    journal = await JournalService().add_comment(db_session, issue, world.admin, "Seagrass meadow extends north.")
    attachment = Attachment(
        container_type="Issue",
        container_id=issue.id,
        filename="seagrass.txt",
        disk_filename="six-seagrass-0001.txt",
        filesize=5,
        author_id=world.admin.id,
        description="Seagrass transect sheet",
    )
    db_session.add(attachment)
    await db_session.commit()

    await IssueService().move(db_session, issue, world.other, world.admin)
    await db_session.commit()

    for source_type, entity_id in (("issue", issue.id), ("journal", journal.id), ("attachment", attachment.id)):
        assert await _source_project(db_session, source_type, entity_id) == world.other.id, source_type
    assert journal.id in await _found(db_session, world.admin, "meadow", "comments", project_id=world.other.id)
    assert journal.id not in await _found(db_session, world.admin, "meadow", "comments", project_id=world.project.id)


# ---------------------------------------------------------------------------
# Import and upgrade
# ---------------------------------------------------------------------------


async def test_import_backfill_indexes_comments_and_attachments(db_session, world, no_model):
    from specivo.importers.core.ir import EntityType
    from specivo.importers.load.search_index import backfill_search_index

    defer_search_index(db_session)
    issue = await _new_issue(db_session, world, "Imported issue")
    journal = await JournalService().add_comment(db_session, issue, world.admin, "Imported comment about tides.")
    attachment = Attachment(
        container_type="Issue",
        container_id=issue.id,
        filename="imported.txt",
        disk_filename="six-imported-0001.txt",
        filesize=5,
        author_id=world.admin.id,
    )
    db_session.add(attachment)
    await db_session.commit()
    assert await _chunks(db_session, "journal", journal.id) == []

    ids = {EntityType.ISSUE: issue.id, EntityType.JOURNAL: journal.id, EntityType.ATTACHMENT: attachment.id}

    async def _preload(_session, entity_type):
        return {"1": ids[entity_type]} if entity_type in ids else {}

    ctx = SimpleNamespace(
        session=db_session,
        options=SimpleNamespace(dry_run=False),
        id_map=SimpleNamespace(preload=_preload),
        warn=lambda message, **context: pytest.fail(f"{message} {context}"),
        tick=lambda: None,
    )
    await backfill_search_index(ctx)

    assert await _chunks(db_session, "issue", issue.id) == ["Imported issue"]
    assert await _chunks(db_session, "journal", journal.id) == ["Imported comment about tides."]
    assert await _chunks(db_session, "attachment", attachment.id) != []


def _load_migration():
    path = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "0037_comment_search_chunks.py"
    spec = importlib.util.spec_from_file_location("migration_0037", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_upgrade_creates_chunks_for_existing_comments(db_session, world, no_model):
    # Comments written before this release have no chunks.
    defer_search_index(db_session)
    issue = await _new_issue(db_session, world, "Older issue")
    human = await JournalService().add_comment(db_session, issue, world.admin, "Older comment about the jetty.")
    short = await JournalService().add_comment(db_session, issue, world.admin, "Short.")
    bot = await JournalService().add_comment(db_session, issue, world.bot, "Older automated jetty status line.")
    await db_session.commit()
    assert await _found(db_session, world.admin, "jetty", "comments") == set()

    migration = _load_migration()
    await db_session.run_sync(lambda s: migration.create_comment_chunks(s.connection(), True, 20, True))
    # Running it again changes nothing.
    await db_session.run_sync(lambda s: migration.create_comment_chunks(s.connection(), True, 20, True))

    assert await _chunks(db_session, "journal", human.id) == ["Older comment about the jetty."]
    assert await _chunks(db_session, "journal", short.id) == []
    assert await _chunks(db_session, "journal", bot.id) == []
    assert await _source_project(db_session, "journal", human.id) == world.project.id
    assert await _found(db_session, world.admin, "jetty", "comments") == {human.id}


# ---------------------------------------------------------------------------
# Attachments on wiki pages in the trash
# ---------------------------------------------------------------------------


async def _semantic_ids(db: AsyncSession, user: User, query: str) -> set[int]:
    results, _total = await _search.semantic_search(db, query, user, limit=50)
    return {r.id for r in results if r.result_type == "attachment"}


class TestTrashedWikiPageAttachments:
    @staticmethod
    async def _page_with_attachment(db: AsyncSession, world: SimpleNamespace) -> tuple:
        page, _ = await WikiService().create_page(db, world.project.id, "Tide Tables", "Monthly tables.", world.admin)
        attachment = Attachment(
            container_type="WikiPage",
            container_id=page.id,
            filename="tides.csv",
            disk_filename="six-tides-0001.csv",
            filesize=5,
            author_id=world.admin.id,
            description="Estuary tide predictions",
        )
        db.add(attachment)
        await db.commit()
        return page, attachment

    async def test_hidden_while_the_page_is_in_the_trash(self, db_session, world, mock_model, queued, monkeypatch):
        from specivo.tasks.embeddings import _embed_sources_async

        TestEmbeddingTask._use_session(monkeypatch, db_session)
        page, attachment = await self._page_with_attachment(db_session, world)
        await _embed_sources_async([("attachment", attachment.id)])
        query = "Estuary tide predictions"
        for user in (world.admin, world.reader):
            assert attachment.id in await _found(db_session, user, "estuary", "attachments")
            assert attachment.id in await _semantic_ids(db_session, user, query)

        await WikiService().delete_page(db_session, page.id, world.admin)
        await db_session.commit()
        assert await _chunks(db_session, "attachment", attachment.id) == []
        for user in (world.admin, world.reader):
            assert attachment.id not in await _found(db_session, user, "estuary", "attachments")
            assert attachment.id not in await _semantic_ids(db_session, user, query)

        await WikiService().restore_page(db_session, page.id)
        await db_session.commit()
        assert await _chunks(db_session, "attachment", attachment.id) != []
        assert ["attachment", attachment.id] in queued[-1]
        await _embed_sources_async([("attachment", attachment.id)])
        for user in (world.admin, world.reader):
            assert attachment.id in await _found(db_session, user, "estuary", "attachments")
            assert attachment.id in await _semantic_ids(db_session, user, query)

    async def test_children_trashed_in_bulk_take_their_attachments(self, db_session, world, no_model):
        wiki = WikiService()
        parent, _ = await wiki.create_page(db_session, world.project.id, "Coastline", "Overview.", world.admin)
        child, _ = await wiki.create_page(
            db_session, world.project.id, "Sea Walls", "Wall list.", world.admin, parent_slug=parent.slug
        )
        attachment = Attachment(
            container_type="WikiPage",
            container_id=child.id,
            filename="walls.csv",
            disk_filename="six-walls-0001.csv",
            filesize=5,
            author_id=world.admin.id,
            description="Sea wall inspection",
        )
        db_session.add(attachment)
        await db_session.commit()

        await wiki.delete_page(db_session, parent.id, world.admin, cascade_children=True)
        await db_session.commit()
        assert await _chunks(db_session, "attachment", attachment.id) == []

    async def test_search_hides_stale_chunks_of_a_trashed_page(
        self, db_session, world, mock_model, queued, monkeypatch
    ):
        """Chunks left from before the fix must not leak through either search mode."""
        from specivo.models.wiki import WikiPage
        from specivo.tasks.embeddings import _embed_sources_async

        TestEmbeddingTask._use_session(monkeypatch, db_session)
        page, attachment = await self._page_with_attachment(db_session, world)
        await _embed_sources_async([("attachment", attachment.id)])
        # Trash the page behind the index's back, as an older release would have.
        await db_session.execute(update(WikiPage).where(WikiPage.id == page.id).values(deleted_at=func.now()))
        await db_session.commit()
        assert await _chunks(db_session, "attachment", attachment.id) != []

        for user in (world.admin, world.reader):
            assert attachment.id not in await _found(db_session, user, "estuary", "attachments")
            assert attachment.id not in await _semantic_ids(db_session, user, "Estuary tide predictions")


async def test_failure_log_does_not_quote_content(db_session, world, no_model, monkeypatch, caplog):
    """A database error's message carries its parameters; the warning must not."""
    from sqlalchemy import text

    secret = "Confidential harbour lease terms"

    def _fails_with_parameters(conn, *_args):
        conn.execute(text("SELECT CAST(:value AS integer)"), {"value": secret})

    monkeypatch.setattr(search_index_service, "load_source_chunks", _fails_with_parameters)
    with caplog.at_level(logging.WARNING, logger="specivo.services.search_index_service"):
        issue = await _new_issue(db_session, world, "Lease review")

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any(f"Search indexing failed for issue {issue.id}" in m for m in warnings), warnings
    assert any("SQLSTATE" in m for m in warnings), warnings
    assert all(secret not in m and "harbour" not in m for m in warnings), warnings


def test_describe_error_names_only_the_class():
    from sqlalchemy.exc import DataError

    from specivo.services.search_index_service import describe_error

    assert describe_error(ValueError("secret text")) == "ValueError"
    described = describe_error(DataError("SELECT :v", {"v": "secret text"}, Exception("bad value secret text")))
    assert "secret" not in described
    assert described.startswith("DataError")
