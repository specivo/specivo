"""Regression tests for the Celery cross-event-loop pool reuse bug.

Async Celery tasks run their coroutine in a *new* event loop on every call
(see :func:`specivo.tasks._async.run_async`). Before the fix the async tasks
used the module-level singleton pooled engine from
:mod:`specivo.core.database` (an ``AsyncAdaptedQueuePool``). asyncpg connections
in that pool stay bound to the loop they were opened on, so when a prefork
worker reused one child process for a second task — running in a *new* loop —
the reused pooled connection raised ``RuntimeError: Event loop is closed`` /
"got Future attached to a different loop".

These tests invoke a task's *sync* entrypoint twice in a row inside one process
(each call opens its own new loop) and assert no exception and that the work
actually happened. On the pre-fix code (singleton ``QueuePool`` engine) the
second call raised "Event loop is closed"; with ``task_session()``'s per-call
``NullPool`` engine both calls succeed.

These tests deliberately bypass the rollback-isolated ``db_session`` fixture:
``task_session()`` opens its own real connection to the test DB via
``get_settings().database_url``, so the fixtures must be *really* committed and
explicitly cleaned up afterwards.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text
from sqlalchemy import event as sa_event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from specivo.core.config import get_settings
from specivo.core.database import _register_ltree_codec, set_engine
from specivo.models.attachment import Attachment
from specivo.models.issue import Issue
from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.project import Project
from specivo.models.search import ChunkEmbedding, EmbeddingModel, SearchChunk, SearchSource
from specivo.models.user import User
from specivo.models.wiki import WikiPageLink
from specivo.schemas.search import SearchSourceType
from specivo.services.wiki_service import WikiService
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import UserFactory

# Serial: these tests use a real (committed) connection to the shared test DB
# and a production-style pooled engine, so they must not run concurrently under
# xdist alongside rollback-isolated tests.
pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.serial]


def _attach_ltree_codec(engine) -> None:  # type: ignore[no-untyped-def]
    @sa_event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_connection, connection_record):  # type: ignore[no-untyped-def]
        dbapi_connection.run_async(_register_ltree_codec)


@pytest_asyncio.fixture
async def real_db_session() -> AsyncIterator[AsyncSession]:
    """A session on a *real* (production-style) pooled engine, committed to disk.

    Installs the engine as the module-level singleton via ``set_engine`` so the
    task under test (pre-fix) reuses this exact pool across loops — reproducing
    the cross-loop bug. The engine is a ``QueuePool`` (default), matching the
    production engine that triggered the original failure.

    Everything created through this session is committed to the shared test DB.
    Because the task under test can poison the pooled connection on the pre-fix
    code, teardown runs on a *fresh* engine and deletes the tracked fixtures in
    FK-safe order so cleanup never depends on the (possibly broken) task pool.
    """
    engine = create_async_engine(get_settings().database_url)
    _attach_ltree_codec(engine)
    set_engine(engine)

    # IDs of committed fixtures, removed in teardown (FK-safe order). Lookups
    # (trackers/statuses/priorities) are NOT project-scoped, so they must be
    # tracked and deleted explicitly or they pollute other tests' default-row
    # lookups in the shared test DB.
    tracked: dict[str, list[int]] = {
        "attachments": [],
        "embedding_models": [],
        "projects": [],
        "trackers": [],
        "statuses": [],
        "priorities": [],
        "users": [],
    }

    factory = async_sessionmaker(engine, expire_on_commit=False)
    session = factory()
    session.info["_tracked"] = tracked
    try:
        yield session
    finally:
        try:
            await session.close()
        except Exception:
            pass
        await engine.dispose()

        # Fresh engine for teardown — never reuse the task-poisoned pool.
        cleanup_engine = create_async_engine(get_settings().database_url)
        _attach_ltree_codec(cleanup_engine)
        async with cleanup_engine.begin() as conn:
            if tracked["attachments"]:
                await conn.execute(delete(Attachment).where(Attachment.id.in_(tracked["attachments"])))
            if tracked["projects"]:
                # Projects cascade to wiki, issues, and search sources/chunks/embeddings.
                await conn.execute(delete(Project).where(Project.id.in_(tracked["projects"])))
            if tracked["embedding_models"]:
                await conn.execute(delete(EmbeddingModel).where(EmbeddingModel.id.in_(tracked["embedding_models"])))
                # Restore the seeded default model that the mock_model fixture un-defaulted.
                await conn.execute(
                    text("UPDATE embedding_models SET is_default = true WHERE name = 'multilingual-e5-small'")
                )
            # Lookups are deleted after projects (which cascade their issues away),
            # so no issue FK still references these tracker/status/priority rows.
            if tracked["trackers"]:
                await conn.execute(delete(Tracker).where(Tracker.id.in_(tracked["trackers"])))
            if tracked["statuses"]:
                await conn.execute(delete(IssueStatus).where(IssueStatus.id.in_(tracked["statuses"])))
            if tracked["priorities"]:
                await conn.execute(delete(IssuePriority).where(IssuePriority.id.in_(tracked["priorities"])))
            if tracked["users"]:
                await conn.execute(delete(User).where(User.id.in_(tracked["users"])))
        await cleanup_engine.dispose()


@asynccontextmanager
async def _fresh_read_session() -> AsyncIterator[AsyncSession]:
    """Read-only verification session on its own fresh NullPool engine.

    The task under test runs in a worker thread and commits via its own engine;
    asserting through a brand-new connection (rather than the setup session,
    whose state may be entangled with the threaded run) reads the committed
    results cleanly on pytest's loop.
    """
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    _attach_ltree_codec(engine)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            yield session
    finally:
        await engine.dispose()


def _run_in_worker_thread(*calls: Callable[[], object]) -> None:
    """Run sync task entrypoints sequentially in ONE worker thread.

    Each call invokes ``run_async`` which opens its own *new* event loop and
    closes it. Running them back to back in a single thread (with no ambient
    running loop) mirrors a Celery prefork worker reusing one child process for
    several tasks — the exact condition that surfaced the cross-loop pool bug.
    A worker thread is used because the test itself runs inside pytest's event
    loop; the task's ``run_until_complete`` needs a thread with no running loop.
    """

    def _runner() -> None:
        for call in calls:
            call()

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(_runner).result()


def _unique_suffix() -> str:
    """Short unique token so committed fixtures never collide across runs."""
    return uuid.uuid4().hex[:8]


async def _make_project(session: AsyncSession, prefix: str) -> Project:
    suffix = _unique_suffix()
    proj = ProjectFactory.build(key=f"{prefix}{suffix}".upper(), identifier=f"{prefix}-{suffix}")
    session.add(proj)
    await session.commit()
    await session.refresh(proj)
    session.info["_tracked"]["projects"].append(proj.id)
    return proj


async def _make_user(session: AsyncSession, prefix: str) -> User:
    user = UserFactory.build(login=f"{prefix}-{_unique_suffix()}", status="active")
    session.add(user)
    await session.commit()
    await session.refresh(user)
    session.info["_tracked"]["users"].append(user.id)
    return user


# ---------------------------------------------------------------------------
# wiki_links — rebuild_wiki_page_links called twice in one process
# ---------------------------------------------------------------------------


class TestWikiLinkTaskLoopReuse:
    async def test_rebuild_links_twice_in_sequence(self, real_db_session: AsyncSession) -> None:
        """Calling the wiki-link rebuild task twice (each in its own new loop)
        succeeds and rebuilds the link graph both times.

        Pre-fix this raised ``RuntimeError: Event loop is closed`` on the second
        call because the singleton pool's asyncpg connection was bound to the
        first (now-closed) loop.
        """
        from specivo.tasks.wiki_links import rebuild_wiki_page_links

        wiki_service = WikiService()
        project = await _make_project(real_db_session, "TLR1")
        author = await _make_user(real_db_session, "tlr_wiki_author")

        wiki = await wiki_service.get_or_create_wiki(real_db_session, project.id)
        page_a, _ = await wiki_service.create_page(real_db_session, project.id, "Page A", "Links to [[Page B]]", author)
        page_b, _ = await wiki_service.create_page(real_db_session, project.id, "Page B", "Body", author)
        await real_db_session.commit()

        # Two back-to-back calls in one worker thread: first opens a new loop
        # and uses the singleton pool; the second opens ANOTHER new loop, and
        # reusing the pooled asyncpg connection from loop 1 is what crashes on
        # the unfixed code.
        _run_in_worker_thread(
            lambda: rebuild_wiki_page_links.run(wiki.id, page_a.id),
            lambda: rebuild_wiki_page_links.run(wiki.id, page_a.id),
        )

        # The link graph was actually rebuilt (one [[Page B]] link, resolved).
        async with _fresh_read_session() as read:
            result = await read.execute(select(WikiPageLink).where(WikiPageLink.source_page_id == page_a.id))
            links = list(result.scalars().all())
        assert len(links) == 1
        assert links[0].target_slug == "page-b"
        assert links[0].target_page_id == page_b.id


# ---------------------------------------------------------------------------
# embeddings — generate_embeddings called twice (ISSUE + ATTACHMENT)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def mock_model(real_db_session: AsyncSession) -> AsyncIterator[EmbeddingModel]:
    """A default mock embedding model committed to the test DB, with cleanup.

    The ``mock`` provider produces deterministic vectors with no external
    dependency, so embedding generation runs end to end against the DB.
    """
    from sqlalchemy import update

    await real_db_session.execute(
        update(EmbeddingModel).where(EmbeddingModel.is_default.is_(True)).values(is_default=False)
    )
    model = EmbeddingModel(
        name=f"loop-reuse-mock-{_unique_suffix()}",
        provider="mock",
        model_name="mock-1536",
        dimensions=1536,
        is_default=True,
    )
    real_db_session.add(model)
    await real_db_session.commit()
    await real_db_session.refresh(model)
    # Removed (and the seeded default restored) by the real_db_session teardown.
    real_db_session.info["_tracked"]["embedding_models"].append(model.id)
    yield model


class TestEmbeddingTaskLoopReuse:
    async def _make_issue(self, session: AsyncSession, project: Project, author: User, subject: str) -> Issue:
        from specivo.schemas.issue import IssueCreate
        from specivo.services.issue_service import IssueService

        tracked = session.info["_tracked"]
        status = StatusFactory.build(name="New", position=1, category="backlog")
        session.add(status)
        await session.flush()
        tracker = TrackerFactory.build(name="Bug", default_status_id=status.id)
        session.add(tracker)
        priority = PriorityFactory.build(name="Normal", is_default=True, position=2)
        session.add(priority)
        await session.commit()
        await session.refresh(tracker)
        await session.refresh(status)
        await session.refresh(priority)
        tracked["statuses"].append(status.id)
        tracked["trackers"].append(tracker.id)
        tracked["priorities"].append(priority.id)

        issue = await IssueService().create(
            session,
            project,
            IssueCreate(project_key=project.key, tracker_id=tracker.id, subject=subject),
            author,
        )
        await session.commit()
        await session.refresh(issue)
        return issue

    async def test_generate_embeddings_twice_issue_then_attachment(
        self, real_db_session: AsyncSession, mock_model: EmbeddingModel
    ) -> None:
        """Calling generate_embeddings twice (ISSUE then ATTACHMENT), each in
        its own new loop, succeeds and persists embeddings for both.

        Pre-fix the second call raised "Event loop is closed" because the
        singleton pool's connection belonged to the first call's closed loop.
        """
        from specivo.tasks.embeddings import generate_embeddings

        project = await _make_project(real_db_session, "TLR2")
        author = await _make_user(real_db_session, "tlr_embed_author")

        issue = await self._make_issue(real_db_session, project, author, "Searchable subject for embeddings")

        attachment = Attachment(
            container_type="Issue",
            container_id=issue.id,
            filename="notes.txt",
            disk_filename=f"tlr2-notes-{_unique_suffix()}.txt",
            content_type="text/plain",
            filesize=42,
            author_id=author.id,
            description="A short attachment description to embed",
        )
        real_db_session.add(attachment)
        await real_db_session.commit()
        await real_db_session.refresh(attachment)
        real_db_session.info["_tracked"]["attachments"].append(attachment.id)

        # Two back-to-back calls in one worker thread: ISSUE first (new loop,
        # singleton pool), then ATTACHMENT (ANOTHER new loop). Reusing the
        # loop-1 pooled connection is what crashes on the unfixed code.
        _run_in_worker_thread(
            lambda: generate_embeddings.run(SearchSourceType.ISSUE.value, issue.id, project.id),
            lambda: generate_embeddings.run(SearchSourceType.ATTACHMENT.value, attachment.id, project.id),
        )

        # Both source entities got embeddings persisted.
        async with _fresh_read_session() as read:
            issue_src = (
                await read.execute(
                    select(SearchSource).where(
                        SearchSource.source_type == SearchSourceType.ISSUE,
                        SearchSource.entity_id == issue.id,
                    )
                )
            ).scalar_one_or_none()
            att_src = (
                await read.execute(
                    select(SearchSource).where(
                        SearchSource.source_type == SearchSourceType.ATTACHMENT,
                        SearchSource.entity_id == attachment.id,
                    )
                )
            ).scalar_one_or_none()

            assert issue_src is not None, "ISSUE embedding source not created"
            assert att_src is not None, "ATTACHMENT embedding source not created"

            for src in (issue_src, att_src):
                chunk_count = (
                    await read.execute(
                        select(text("count(*)")).select_from(SearchChunk).where(SearchChunk.source_id == src.id)
                    )
                ).scalar_one()
                assert chunk_count >= 1
                emb_count = (
                    await read.execute(
                        select(text("count(*)"))
                        .select_from(ChunkEmbedding)
                        .join(SearchChunk, SearchChunk.id == ChunkEmbedding.chunk_id)
                        .where(SearchChunk.source_id == src.id)
                    )
                ).scalar_one()
                assert emb_count >= 1
