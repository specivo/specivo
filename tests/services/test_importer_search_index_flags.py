"""Regression tests for the bulk-import search-indexing flags.

``IssueService.create`` and ``WikiService.create_page`` embed content inline and
(for wiki) dispatch a link-graph task. The import pipeline turns both off and
does that work once at the end of a run. These tests pin both halves of the
contract: the flags suppress the work, and leaving them out changes nothing for
every existing caller.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.project import Project
from specivo.models.user import User
from specivo.schemas.issue import IssueCreate
from specivo.services.embedding_service import EmbeddingService
from specivo.services.issue_service import IssueService
from specivo.services.wiki_service import WikiService
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.service]


@pytest.fixture
def embed_calls(monkeypatch) -> list[tuple]:
    """Record every ``EmbeddingService.embed_source`` call instead of running it."""
    calls: list[tuple] = []

    async def _record(self, session, source_type, entity_id, project_id, chunks, model_id=None):
        calls.append((source_type, entity_id, project_id))
        return None

    monkeypatch.setattr(EmbeddingService, "embed_source", _record)
    return calls


@pytest.fixture
def link_rebuild_calls(monkeypatch) -> list[tuple]:
    """Record link-graph task dispatches instead of talking to the broker."""
    from specivo.tasks.wiki_links import rebuild_wiki_page_links

    calls: list[tuple] = []
    monkeypatch.setattr(
        rebuild_wiki_page_links,
        "delay",
        lambda wiki_id, page_id: calls.append((wiki_id, page_id)),
    )
    return calls


@pytest_asyncio.fixture
async def status(db_session: AsyncSession):
    row = StatusFactory.build(name="New", position=1, category="backlog")
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


@pytest_asyncio.fixture
async def tracker(db_session: AsyncSession, status):
    row = TrackerFactory.build(name="Bug", default_status_id=status.id)
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


@pytest_asyncio.fixture
async def priority(db_session: AsyncSession):
    row = PriorityFactory.build(name="Normal", is_default=True, position=2)
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


@pytest_asyncio.fixture
async def project(db_session: AsyncSession) -> Project:
    row = ProjectFactory.build(key="IMPFLAG", name="Import Flag Test", is_public=True)
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


@pytest_asyncio.fixture
async def author(db_session: AsyncSession) -> User:
    row = AdminUserFactory.build(login="impflag_admin", status="active")
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


class TestIssueSkipSearchIndex:
    async def test_indexes_by_default(self, db_session, project, tracker, priority, author, embed_calls):
        await IssueService().create(
            db_session,
            project,
            IssueCreate(project_key=project.key, tracker_id=tracker.id, subject="Indexed by default"),
            author,
        )
        assert len(embed_calls) == 1

    async def test_skips_when_flag_is_set(self, db_session, project, tracker, priority, author, embed_calls):
        await IssueService().create(
            db_session,
            project,
            IssueCreate(project_key=project.key, tracker_id=tracker.id, subject="Not indexed"),
            author,
            skip_search_index=True,
        )
        assert embed_calls == []

    async def test_issue_is_still_created_when_indexing_is_skipped(
        self, db_session, project, tracker, priority, author, embed_calls
    ):
        """Skipping the index must not change what the row looks like."""
        issue = await IssueService().create(
            db_session,
            project,
            IssueCreate(
                project_key=project.key,
                tracker_id=tracker.id,
                subject="Still a real issue",
                description="Body text",
            ),
            author,
            skip_search_index=True,
        )
        assert issue.id is not None
        assert issue.display_key == f"{project.key}-{issue.sequence_number}"
        assert issue.description == "Body text"


class TestWikiSkipFlags:
    async def test_indexes_and_dispatches_by_default(
        self, db_session, project, author, embed_calls, link_rebuild_calls
    ):
        await WikiService().create_page(db_session, project.id, "Default Page", "Body", author)
        assert len(embed_calls) == 1
        assert len(link_rebuild_calls) == 1

    async def test_skip_search_index_leaves_link_rebuild_alone(
        self, db_session, project, author, embed_calls, link_rebuild_calls
    ):
        """The two flags are independent; setting one must not imply the other."""
        await WikiService().create_page(db_session, project.id, "No Embedding", "Body", author, skip_search_index=True)
        assert embed_calls == []
        assert len(link_rebuild_calls) == 1

    async def test_skip_link_rebuild_leaves_indexing_alone(
        self, db_session, project, author, embed_calls, link_rebuild_calls
    ):
        await WikiService().create_page(
            db_session, project.id, "No Link Rebuild", "Body", author, skip_link_rebuild=True
        )
        assert len(embed_calls) == 1
        assert link_rebuild_calls == []

    async def test_both_flags_suppress_all_side_work(
        self, db_session, project, author, embed_calls, link_rebuild_calls
    ):
        page, content = await WikiService().create_page(
            db_session,
            project.id,
            "Imported Page",
            "Body",
            author,
            skip_search_index=True,
            skip_link_rebuild=True,
        )
        assert embed_calls == []
        assert link_rebuild_calls == []
        assert page.id is not None
        assert content.version == 1
        assert content.text == "Body"
