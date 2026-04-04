"""Integration tests for the built-in MCP server tools.

Tests call the internal ``_*`` functions directly with explicit session
and user, bypassing MCP transport.  This validates that the service-layer
wrappers produce correct string output without requiring stdio transport.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.project import Project
from specivo.models.user import User
from specivo.schemas.issue import IssueCreate
from specivo.services.issue_service import IssueService
from specivo.services.wiki_service import WikiService
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory

pytestmark = pytest.mark.asyncio(loop_scope="function")

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def status(db_session: AsyncSession) -> IssueStatus:
    s = StatusFactory.build(name="New", position=1, is_closed=False)
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


@pytest_asyncio.fixture
async def closed_status(db_session: AsyncSession) -> IssueStatus:
    s = StatusFactory.build(name="Closed", position=5, is_closed=True)
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


@pytest_asyncio.fixture
async def tracker(db_session: AsyncSession, status: IssueStatus) -> Tracker:
    t = TrackerFactory.build(name="Bug", default_status_id=status.id)
    db_session.add(t)
    await db_session.commit()
    await db_session.refresh(t)
    return t


@pytest_asyncio.fixture
async def priority(db_session: AsyncSession) -> IssuePriority:
    p = PriorityFactory.build(name="Normal", is_default=True, position=2)
    db_session.add(p)
    await db_session.commit()
    await db_session.refresh(p)
    return p


@pytest_asyncio.fixture
async def project(db_session: AsyncSession) -> Project:
    proj = ProjectFactory.build(key="ACME", identifier="acme-app")
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


@pytest_asyncio.fixture
async def admin(db_session: AsyncSession) -> User:
    user = AdminUserFactory.build(login="mcp_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def seed(status, tracker, priority, project):
    """Ensure all lookups are seeded so issue creation works."""
    return {"status": status, "tracker": tracker, "priority": priority, "project": project}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMcpListProjects:
    async def test_list_projects(self, db_session: AsyncSession, admin: User, project: Project):
        from specivo.mcp.tools import _list_projects

        result = await _list_projects(db_session, admin, limit=500)
        assert project.key in result
        assert project.name in result


class TestMcpCreateIssue:
    async def test_create_issue(self, db_session: AsyncSession, admin: User, seed: dict):
        from specivo.mcp.tools import _create_issue

        result = await _create_issue(
            session=db_session,
            user=admin,
            project_key="ACME",
            tracker_id=seed["tracker"].id,
            subject="Test MCP issue",
            description="Created via MCP tool",
        )
        assert "ACME-1" in result
        assert "Test MCP issue" in result


class TestMcpShowIssue:
    async def test_show_issue(self, db_session: AsyncSession, admin: User, seed: dict):
        from specivo.mcp.tools import _show_issue

        # Create an issue first
        svc = IssueService()
        issue = await svc.create(
            db_session,
            seed["project"],
            IssueCreate(
                project_key="ACME",
                tracker_id=seed["tracker"].id,
                subject="Show me",
                description="Detailed description here",
            ),
            admin,
        )
        await db_session.commit()

        result = await _show_issue(db_session, admin, issue.display_key)
        assert "Show me" in result
        assert "Detailed description here" in result
        assert "ACME-1" in result

    async def test_show_issue_metadata_only(self, db_session: AsyncSession, admin: User, seed: dict):
        from specivo.mcp.tools import _show_issue

        svc = IssueService()
        issue = await svc.create(
            db_session,
            seed["project"],
            IssueCreate(
                project_key="ACME",
                tracker_id=seed["tracker"].id,
                subject="Metadata only test",
                description="This long description should NOT appear in metadata_only mode",
            ),
            admin,
        )
        await db_session.commit()

        result = await _show_issue(db_session, admin, issue.display_key, metadata_only=True)
        assert "Metadata only test" in result
        assert "This long description should NOT appear" not in result

    async def test_show_issue_search_param(self, db_session: AsyncSession, admin: User, seed: dict):
        from specivo.mcp.tools import _show_issue

        svc = IssueService()
        description = (
            "## Introduction\n\n"
            "This is the intro paragraph with some background.\n\n"
            "## Implementation\n\n"
            "The specific_keyword_to_find lives in this section.\n"
            "More implementation details follow here.\n\n"
            "## Testing\n\n"
            "Tests should cover all edge cases."
        )
        issue = await svc.create(
            db_session,
            seed["project"],
            IssueCreate(
                project_key="ACME",
                tracker_id=seed["tracker"].id,
                subject="Search param test",
                description=description,
            ),
            admin,
        )
        await db_session.commit()

        result = await _show_issue(db_session, admin, issue.display_key, search="specific_keyword_to_find")
        assert "specific_keyword_to_find" in result
        # The unrelated section should not be fully included
        assert "Tests should cover all edge cases" not in result


class TestMcpListIssues:
    async def test_list_issues(self, db_session: AsyncSession, admin: User, seed: dict):
        from specivo.mcp.tools import _list_issues

        svc = IssueService()
        await svc.create(
            db_session,
            seed["project"],
            IssueCreate(
                project_key="ACME",
                tracker_id=seed["tracker"].id,
                subject="First issue",
            ),
            admin,
        )
        await svc.create(
            db_session,
            seed["project"],
            IssueCreate(
                project_key="ACME",
                tracker_id=seed["tracker"].id,
                subject="Second issue",
            ),
            admin,
        )
        await db_session.commit()

        result = await _list_issues(db_session, admin, project_key="ACME")
        assert "ACME-1" in result
        assert "ACME-2" in result
        assert "First issue" in result
        assert "Second issue" in result


class TestMcpUpdateIssue:
    async def test_update_issue(self, db_session: AsyncSession, admin: User, seed: dict):
        from specivo.mcp.tools import _update_issue

        svc = IssueService()
        issue = await svc.create(
            db_session,
            seed["project"],
            IssueCreate(
                project_key="ACME",
                tracker_id=seed["tracker"].id,
                subject="Original subject",
            ),
            admin,
        )
        await db_session.commit()

        result = await _update_issue(db_session, admin, issue.display_key, subject="Updated subject")
        assert "Updated subject" in result
        assert "ACME-1" in result


class TestMcpEditDescription:
    async def test_edit_description(self, db_session: AsyncSession, admin: User, seed: dict):
        from specivo.mcp.tools import _edit_description

        svc = IssueService()
        issue = await svc.create(
            db_session,
            seed["project"],
            IssueCreate(
                project_key="ACME",
                tracker_id=seed["tracker"].id,
                subject="Edit desc test",
                description="Hello world, this is a test.",
            ),
            admin,
        )
        await db_session.commit()

        result = await _edit_description(db_session, admin, issue.display_key, "Hello world", "Goodbye world")
        assert "Goodbye world" in result

        # Verify the change persisted
        await db_session.refresh(issue)
        assert "Goodbye world" in issue.description
        assert "Hello world" not in issue.description

    async def test_edit_description_not_found(self, db_session: AsyncSession, admin: User, seed: dict):
        from specivo.mcp.tools import _edit_description

        svc = IssueService()
        issue = await svc.create(
            db_session,
            seed["project"],
            IssueCreate(
                project_key="ACME",
                tracker_id=seed["tracker"].id,
                subject="Edit desc not found",
                description="Some content here.",
            ),
            admin,
        )
        await db_session.commit()

        result = await _edit_description(db_session, admin, issue.display_key, "nonexistent text", "replacement")
        assert "not found" in result.lower()


class TestMcpSearch:
    async def test_search(self, db_session: AsyncSession, admin: User, seed: dict):
        from specivo.mcp.tools import _search

        svc = IssueService()
        await svc.create(
            db_session,
            seed["project"],
            IssueCreate(
                project_key="ACME",
                tracker_id=seed["tracker"].id,
                subject="Searchable unique banana topic",
            ),
            admin,
        )
        await db_session.commit()

        result = await _search(db_session, admin, query="banana")
        assert "banana" in result.lower()


class TestMcpWiki:
    async def test_read_wiki(self, db_session: AsyncSession, admin: User, project: Project):
        from specivo.mcp.tools import _read_wiki

        wiki_svc = WikiService()
        await wiki_svc.create_page(db_session, project.id, "Test Page", "Wiki content here", admin)
        await db_session.commit()

        result = await _read_wiki(db_session, admin, "ACME", "test-page")
        assert "Wiki content here" in result
        assert "Test Page" in result

    async def test_read_wiki_metadata_only(self, db_session: AsyncSession, admin: User, project: Project):
        from specivo.mcp.tools import _read_wiki

        wiki_svc = WikiService()
        await wiki_svc.create_page(
            db_session,
            project.id,
            "Meta Wiki",
            "This body should not appear in metadata only mode",
            admin,
        )
        await db_session.commit()

        result = await _read_wiki(db_session, admin, "ACME", "meta-wiki", metadata_only=True)
        assert "Meta Wiki" in result
        assert "This body should not appear" not in result

    async def test_list_wiki_pages(self, db_session: AsyncSession, admin: User, project: Project):
        from specivo.mcp.tools import _list_wiki_pages

        wiki_svc = WikiService()
        await wiki_svc.create_page(db_session, project.id, "Page A", "AAA", admin)
        await wiki_svc.create_page(db_session, project.id, "Page B", "BBB", admin)
        await db_session.commit()

        result = await _list_wiki_pages(db_session, admin, "ACME")
        assert "Page A" in result
        assert "Page B" in result

    async def test_edit_wiki(self, db_session: AsyncSession, admin: User, project: Project):
        from specivo.mcp.tools import _edit_wiki

        wiki_svc = WikiService()
        await wiki_svc.create_page(db_session, project.id, "Editable", "Old content here.", admin)
        await db_session.commit()

        result = await _edit_wiki(db_session, admin, "ACME", "editable", "Old content", "New content")
        assert "New content" in result


class TestMcpAddComment:
    async def test_add_comment(self, db_session: AsyncSession, admin: User, seed: dict):
        from specivo.mcp.tools import _add_comment

        svc = IssueService()
        issue = await svc.create(
            db_session,
            seed["project"],
            IssueCreate(
                project_key="ACME",
                tracker_id=seed["tracker"].id,
                subject="Comment test",
            ),
            admin,
        )
        await db_session.commit()

        result = await _add_comment(db_session, admin, issue.display_key, "This is a test comment")
        assert "comment" in result.lower() or "journal" in result.lower()
        assert "ACME-1" in result
