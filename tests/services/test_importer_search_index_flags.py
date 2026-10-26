"""Regression tests for the bulk-import search-indexing switches.

Content is indexed by session events as it is written. The import pipeline
marks its sessions with ``defer_search_index`` and indexes everything once at
the end; ``WikiService.create_page`` also takes ``skip_link_rebuild`` so the
link graph is rebuilt once too. These tests pin both halves of the contract:
the switches suppress the work, and leaving them out changes nothing for
every other caller.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.project import Project
from specivo.models.search import SearchSource
from specivo.models.user import User
from specivo.schemas.issue import IssueCreate
from specivo.services.issue_service import IssueService
from specivo.services.search_index_service import defer_search_index
from specivo.services.wiki_service import WikiService
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.service]


async def _source_count(db: AsyncSession, source_type: str, entity_id: int) -> int:
    stmt = select(func.count()).where(SearchSource.source_type == source_type, SearchSource.entity_id == entity_id)
    return (await db.execute(stmt)).scalar_one()


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


class TestIssueDeferredIndex:
    async def test_indexes_by_default(self, db_session, project, tracker, priority, author):
        issue = await IssueService().create(
            db_session,
            project,
            IssueCreate(project_key=project.key, tracker_id=tracker.id, subject="Indexed by default"),
            author,
        )
        assert await _source_count(db_session, "issue", issue.id) == 1

    async def test_skips_when_session_is_deferred(self, db_session, project, tracker, priority, author):
        defer_search_index(db_session)
        issue = await IssueService().create(
            db_session,
            project,
            IssueCreate(project_key=project.key, tracker_id=tracker.id, subject="Not indexed"),
            author,
        )
        assert await _source_count(db_session, "issue", issue.id) == 0

    async def test_issue_is_still_created_when_indexing_is_deferred(
        self, db_session, project, tracker, priority, author
    ):
        """Deferring the index must not change what the row looks like."""
        defer_search_index(db_session)
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
        )
        assert issue.id is not None
        assert issue.display_key == f"{project.key}-{issue.sequence_number}"
        assert issue.description == "Body text"


class TestWikiSkipFlags:
    async def test_indexes_and_dispatches_by_default(self, db_session, project, author, link_rebuild_calls):
        page, _ = await WikiService().create_page(db_session, project.id, "Default Page", "Body", author)
        assert await _source_count(db_session, "wiki_page", page.id) == 1
        assert len(link_rebuild_calls) == 1

    async def test_deferred_index_leaves_link_rebuild_alone(self, db_session, project, author, link_rebuild_calls):
        """The two switches are independent; setting one must not imply the other."""
        defer_search_index(db_session)
        page, _ = await WikiService().create_page(db_session, project.id, "No Index", "Body", author)
        assert await _source_count(db_session, "wiki_page", page.id) == 0
        assert len(link_rebuild_calls) == 1

    async def test_skip_link_rebuild_leaves_indexing_alone(self, db_session, project, author, link_rebuild_calls):
        page, _ = await WikiService().create_page(
            db_session, project.id, "No Link Rebuild", "Body", author, skip_link_rebuild=True
        )
        assert await _source_count(db_session, "wiki_page", page.id) == 1
        assert link_rebuild_calls == []

    async def test_both_switches_suppress_all_side_work(self, db_session, project, author, link_rebuild_calls):
        defer_search_index(db_session)
        page, content = await WikiService().create_page(
            db_session,
            project.id,
            "Imported Page",
            "Body",
            author,
            skip_link_rebuild=True,
        )
        assert await _source_count(db_session, "wiki_page", page.id) == 0
        assert link_rebuild_calls == []
        assert page.id is not None
        assert content.version == 1
        assert content.text == "Body"
