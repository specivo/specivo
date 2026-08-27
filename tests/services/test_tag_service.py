"""Service-layer tests for TagService batch lookups."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.project import Project
from specivo.models.user import User
from specivo.schemas.issue import IssueCreate
from specivo.services.issue_service import IssueService
from specivo.services.tag_service import TagService
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.service]


@pytest_asyncio.fixture
async def status(db_session: AsyncSession):
    s = StatusFactory.build(name="New", position=1, category="backlog")
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


@pytest_asyncio.fixture
async def tracker(db_session: AsyncSession, status):
    t = TrackerFactory.build(name="Bug", default_status_id=status.id)
    db_session.add(t)
    await db_session.commit()
    await db_session.refresh(t)
    return t


@pytest_asyncio.fixture
async def priority(db_session: AsyncSession):
    p = PriorityFactory.build(name="Normal", is_default=True, position=2)
    db_session.add(p)
    await db_session.commit()
    await db_session.refresh(p)
    return p


@pytest_asyncio.fixture
async def project(db_session: AsyncSession) -> Project:
    proj = ProjectFactory.build(key="TSVC", name="Tag Service Test", is_public=True)
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


@pytest_asyncio.fixture
async def admin(db_session: AsyncSession) -> User:
    user = AdminUserFactory.build(login="tsvc_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def _make_issue(db_session: AsyncSession, project, tracker, admin, subject: str):
    issue = await IssueService().create(
        db_session,
        project,
        IssueCreate(project_key=project.key, tracker_id=tracker.id, subject=subject),
        admin,
    )
    await db_session.commit()
    await db_session.refresh(issue)
    return issue


class _ExplodingSession:
    """Stands in for an AsyncSession and fails loudly if a query is issued."""

    async def execute(self, *args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("tags_for_issues must not query the database for an empty id list")


class TestTagsForIssues:
    async def test_empty_input_returns_empty_mapping_without_querying(self):
        """An empty id list short-circuits — no session use at all."""
        svc = TagService()
        result = await svc.tags_for_issues(_ExplodingSession(), [])  # type: ignore[arg-type]
        assert result == {}

    async def test_groups_tags_per_issue(self, db_session, project, tracker, priority, status, admin):
        svc = TagService()
        first = await _make_issue(db_session, project, tracker, admin, "Batched one")
        second = await _make_issue(db_session, project, tracker, admin, "Batched two")
        untagged = await _make_issue(db_session, project, tracker, admin, "Batched three")

        await svc.add_to_issue(db_session, project, first.id, "backend", admin)
        await svc.add_to_issue(db_session, project, first.id, "urgent", admin)
        await svc.add_to_issue(db_session, project, second.id, "docs", admin)
        await db_session.commit()

        result = await svc.tags_for_issues(db_session, [first.id, second.id, untagged.id])

        assert [t.name for t in result[first.id]] == ["backend", "urgent"]
        assert [t.name for t in result[second.id]] == ["docs"]
        assert result[untagged.id] == []

    async def test_untagged_issue_has_an_entry(self, db_session, project, tracker, priority, status, admin):
        svc = TagService()
        issue = await _make_issue(db_session, project, tracker, admin, "No tags here")

        result = await svc.tags_for_issues(db_session, [issue.id])

        assert result == {issue.id: []}

    async def test_matches_single_issue_ordering(self, db_session, project, tracker, priority, status, admin):
        """Batch ordering must match the single-issue helper (case-insensitive by name)."""
        svc = TagService()
        issue = await _make_issue(db_session, project, tracker, admin, "Ordering check")
        for name in ("Zebra", "alpha", "Mango"):
            await svc.add_to_issue(db_session, project, issue.id, name, admin)
        await db_session.commit()

        single = await svc.tags_for_issue(db_session, issue.id)
        batched = await svc.tags_for_issues(db_session, [issue.id])

        assert [t.name for t in batched[issue.id]] == [t.name for t in single]
        assert [t.name for t in single] == ["alpha", "Mango", "Zebra"]
