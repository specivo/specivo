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
from tests.factories.user import AdminUserFactory, UserFactory

pytestmark = pytest.mark.asyncio(loop_scope="function")

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def status(db_session: AsyncSession) -> IssueStatus:
    s = StatusFactory.build(name="New", position=1, category="backlog")
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


@pytest_asyncio.fixture
async def closed_status(db_session: AsyncSession) -> IssueStatus:
    s = StatusFactory.build(name="Closed", position=5, category="closed")
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


class TestMcpWhoami:
    async def test_whoami_returns_user_identity(self, db_session: AsyncSession, admin: User):
        from specivo.mcp.tools import _whoami

        result = await _whoami(db_session, admin)
        assert f"user_id: {admin.id}" in result
        assert f"login: {admin.login}" in result
        assert f"display_name: {admin.display_name}" in result
        assert f"email: {admin.email}" in result
        assert f"is_admin: {admin.is_admin}" in result
        assert f"status: {admin.status}" in result

    async def test_whoami_all_fields_present(self, db_session: AsyncSession, admin: User):
        from specivo.mcp.tools import _whoami

        result = await _whoami(db_session, admin)
        for field in ("user_id:", "login:", "display_name:", "email:", "is_admin:", "status:"):
            assert field in result, f"Missing field {field!r} in whoami output"

    async def test_whoami_non_admin_user(self, db_session: AsyncSession):
        from specivo.mcp.tools import _whoami

        user = UserFactory.build(login="regular_joe", status="active", is_admin=False)
        db_session.add(user)
        await db_session.commit()
        await db_session.refresh(user)

        result = await _whoami(db_session, user)
        assert "login: regular_joe" in result
        assert "is_admin: False" in result


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


class TestMcpListIssuesMetadataFilters:
    async def _seed_three(self, db_session: AsyncSession, admin: User, seed: dict):
        svc = IssueService()
        a = await svc.create(
            db_session,
            seed["project"],
            IssueCreate(project_key="ACME", tracker_id=seed["tracker"].id, subject="alpha"),
            admin,
        )
        b = await svc.create(
            db_session,
            seed["project"],
            IssueCreate(project_key="ACME", tracker_id=seed["tracker"].id, subject="bravo"),
            admin,
        )
        c = await svc.create(
            db_session,
            seed["project"],
            IssueCreate(project_key="ACME", tracker_id=seed["tracker"].id, subject="charlie"),
            admin,
        )
        # alpha: scalar match; bravo: array match; charlie: no match
        a.issue_metadata = {"component": "frontend"}
        b.issue_metadata = {"component": ["frontend", "backend"], "tag": "p0"}
        c.issue_metadata = {"component": "backend"}
        await db_session.commit()
        return a, b, c

    async def test_filter_scalar_equality(self, db_session, admin, seed):
        from specivo.mcp.tools import _list_issues

        a, b, c = await self._seed_three(db_session, admin, seed)
        result = await _list_issues(
            db_session, admin, project_key="ACME", metadata_filters=["component=frontend"]
        )
        assert a.display_key in result
        assert b.display_key in result  # array contains "frontend"
        assert c.display_key not in result
        assert "metadata=component=frontend" in result

    async def test_filter_array_containment_only(self, db_session, admin, seed):
        from specivo.mcp.tools import _list_issues

        _a, b, _c = await self._seed_three(db_session, admin, seed)
        result = await _list_issues(
            db_session, admin, project_key="ACME", metadata_filters=["component=backend"]
        )
        # Only bravo has "backend" in its component array; charlie has component="backend"
        # as a scalar so also matches.
        assert b.display_key in result
        assert _c.display_key in result
        assert _a.display_key not in result

    async def test_multiple_filters_anded(self, db_session, admin, seed):
        from specivo.mcp.tools import _list_issues

        _a, b, _c = await self._seed_three(db_session, admin, seed)
        result = await _list_issues(
            db_session,
            admin,
            project_key="ACME",
            metadata_filters=["component=frontend", "tag=p0"],
        )
        assert b.display_key in result
        assert _a.display_key not in result  # has component=frontend but no tag=p0
        assert _c.display_key not in result

    async def test_no_match_returns_none(self, db_session, admin, seed):
        from specivo.mcp.tools import _list_issues

        await self._seed_three(db_session, admin, seed)
        result = await _list_issues(
            db_session, admin, project_key="ACME", metadata_filters=["component=nonexistent"]
        )
        assert "(none)" in result

    async def test_empty_filter_list_is_noop(self, db_session, admin, seed):
        from specivo.mcp.tools import _list_issues

        a, b, c = await self._seed_three(db_session, admin, seed)
        result = await _list_issues(
            db_session, admin, project_key="ACME", metadata_filters=[]
        )
        assert a.display_key in result
        assert b.display_key in result
        assert c.display_key in result

    async def test_malformed_filter_returns_error(self, db_session, admin, seed):
        from specivo.mcp.tools import _list_issues

        await self._seed_three(db_session, admin, seed)
        result = await _list_issues(
            db_session, admin, project_key="ACME", metadata_filters=["no-equals-sign"]
        )
        assert result.startswith("Error")
        assert "key=value" in result

    async def test_empty_key_returns_error(self, db_session, admin, seed):
        from specivo.mcp.tools import _list_issues

        await self._seed_three(db_session, admin, seed)
        result = await _list_issues(
            db_session, admin, project_key="ACME", metadata_filters=["=value"]
        )
        assert result.startswith("Error")
        assert "empty key" in result

    async def test_value_with_special_chars_safely_escaped(self, db_session, admin, seed):
        from specivo.mcp.tools import _list_issues

        # A value with quotes/backslashes must not allow SQL injection nor break
        # the JSONB literal builder.
        a, _b, _c = await self._seed_three(db_session, admin, seed)
        a.issue_metadata = {"name": 'has "quotes" and \\ backslash'}
        await db_session.commit()
        result = await _list_issues(
            db_session,
            admin,
            project_key="ACME",
            metadata_filters=['name=has "quotes" and \\ backslash'],
        )
        assert a.display_key in result


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
    async def _seed_banana(self, db_session: AsyncSession, admin: User, seed: dict) -> None:
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

    async def test_search_keyword(self, db_session: AsyncSession, admin: User, seed: dict):
        from specivo.mcp.tools import _search

        await self._seed_banana(db_session, admin, seed)

        result = await _search(db_session, admin, query="banana", mode="keyword")
        assert "banana" in result.lower()
        assert "mode=keyword" in result

    async def test_search_default_is_hybrid(self, db_session: AsyncSession, admin: User, seed: dict):
        from specivo.mcp.tools import _search

        await self._seed_banana(db_session, admin, seed)

        result = await _search(db_session, admin, query="banana")
        assert "mode=hybrid" in result

    async def test_search_hybrid_explicit(self, db_session: AsyncSession, admin: User, seed: dict):
        from specivo.mcp.tools import _search

        await self._seed_banana(db_session, admin, seed)

        result = await _search(db_session, admin, query="banana", mode="hybrid")
        assert "mode=hybrid" in result

    async def test_search_semantic_mode_runs(self, db_session: AsyncSession, admin: User, seed: dict):
        from specivo.mcp.tools import _search

        await self._seed_banana(db_session, admin, seed)

        result = await _search(db_session, admin, query="banana", mode="semantic")
        assert "mode=semantic" in result

    async def test_search_invalid_mode(self, db_session: AsyncSession, admin: User, seed: dict):
        from specivo.core.exceptions import ValidationError
        from specivo.mcp.tools import _search

        with pytest.raises(ValidationError):
            await _search(db_session, admin, query="banana", mode="bogus")


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

    async def test_read_wiki_with_search(self, db_session: AsyncSession, admin: User, project: Project):
        from specivo.mcp.tools import _read_wiki

        wiki_svc = WikiService()
        text = "## Introduction\n\nGeneral overview.\n\n## Database Schema\n\nTables and columns here."
        await wiki_svc.create_page(db_session, project.id, "Search Wiki", text, admin)
        await db_session.commit()

        result = await _read_wiki(db_session, admin, "ACME", "search-wiki", search="Tables and columns")
        assert "Database Schema" in result
        assert "Tables and columns" in result
        # Should be the filtered section, not the full content
        assert "section matching" in result


class TestMcpWikiSections:
    """Tests for section-based wiki operations."""

    _WIKI_TEXT = (
        "# Page Title\n\nIntro paragraph.\n\n"
        "## Design\n\nDesign details here.\n\n"
        "### Components\n\nComponent list.\n\n"
        "## Testing\n\nTesting notes.\n\n"
        "## Deployment\n\nDeploy instructions."
    )

    async def test_append_wiki_end(self, db_session: AsyncSession, admin: User, project: Project):
        from specivo.mcp.tools import _append_wiki

        wiki_svc = WikiService()
        await wiki_svc.create_page(db_session, project.id, "Append End", "Initial content.", admin)
        await db_session.commit()

        result = await _append_wiki(db_session, admin, "ACME", "append-end", "## New Section\n\nNew stuff.")
        assert "Appended" in result

        # Verify content
        from specivo.mcp.tools import _read_wiki

        page_result = await _read_wiki(db_session, admin, "ACME", "append-end")
        assert "Initial content." in page_result
        assert "New stuff." in page_result

    async def test_append_wiki_after_heading(self, db_session: AsyncSession, admin: User, project: Project):
        from specivo.mcp.tools import _append_wiki, _read_wiki

        wiki_svc = WikiService()
        await wiki_svc.create_page(db_session, project.id, "Append Heading", self._WIKI_TEXT, admin)
        await db_session.commit()

        result = await _append_wiki(
            db_session,
            admin,
            "ACME",
            "append-heading",
            "Extra design notes.",
            position="after:## Design",
        )
        assert "Appended" in result
        assert "after:## Design" in result

        page_result = await _read_wiki(db_session, admin, "ACME", "append-heading")
        # "Extra design notes." should appear between Design section and Testing section
        assert "Extra design notes." in page_result
        # Verify ordering: Design content, then extra notes, then Testing
        text = page_result
        design_pos = text.index("Design details here.")
        extra_pos = text.index("Extra design notes.")
        testing_pos = text.index("Testing notes.")
        assert design_pos < extra_pos < testing_pos

    async def test_read_wiki_section(self, db_session: AsyncSession, admin: User, project: Project):
        from specivo.mcp.tools import _read_wiki_section

        wiki_svc = WikiService()
        await wiki_svc.create_page(db_session, project.id, "Section Read", self._WIKI_TEXT, admin)
        await db_session.commit()

        # Read Design section with children
        result = await _read_wiki_section(db_session, admin, "ACME", "section-read", "## Design")
        assert "Design details here." in result
        assert "Component list." in result  # child section included
        assert "Testing notes." not in result  # next same-level section excluded

    async def test_read_wiki_section_no_children(self, db_session: AsyncSession, admin: User, project: Project):
        from specivo.mcp.tools import _read_wiki_section

        wiki_svc = WikiService()
        await wiki_svc.create_page(db_session, project.id, "Section NoChild", self._WIKI_TEXT, admin)
        await db_session.commit()

        result = await _read_wiki_section(
            db_session,
            admin,
            "ACME",
            "section-nochild",
            "## Design",
            include_children=False,
        )
        assert "Design details here." in result
        assert "Component list." not in result  # child section excluded

    async def test_read_wiki_section_bare_heading(self, db_session: AsyncSession, admin: User, project: Project):
        from specivo.mcp.tools import _read_wiki_section

        wiki_svc = WikiService()
        await wiki_svc.create_page(db_session, project.id, "Bare Head", self._WIKI_TEXT, admin)
        await db_session.commit()

        # Search by bare heading (no ## prefix)
        result = await _read_wiki_section(db_session, admin, "ACME", "bare-head", "Testing")
        assert "Testing notes." in result

    async def test_replace_wiki_section(self, db_session: AsyncSession, admin: User, project: Project):
        from specivo.mcp.tools import _read_wiki, _replace_wiki_section

        wiki_svc = WikiService()
        await wiki_svc.create_page(db_session, project.id, "Replace Sec", self._WIKI_TEXT, admin)
        await db_session.commit()

        result = await _replace_wiki_section(
            db_session,
            admin,
            "ACME",
            "replace-sec",
            "## Testing",
            "Completely new testing content.",
        )
        assert "Replaced section" in result

        page_result = await _read_wiki(db_session, admin, "ACME", "replace-sec")
        assert "Completely new testing content." in page_result
        assert "Testing notes." not in page_result  # old content gone
        # Other sections preserved
        assert "Design details here." in page_result
        assert "Deploy instructions." in page_result

    async def test_append_wiki_nonexistent_heading(self, db_session: AsyncSession, admin: User, project: Project):
        from specivo.mcp.tools import _append_wiki

        wiki_svc = WikiService()
        await wiki_svc.create_page(db_session, project.id, "No Head", "Some content.", admin)
        await db_session.commit()

        result = await _append_wiki(
            db_session,
            admin,
            "ACME",
            "no-head",
            "Extra.",
            position="after:## Nonexistent",
        )
        assert "Error" in result
        assert "not found" in result


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


class TestMcpListComments:
    """Tests for ``_list_comments`` and comment-count in ``_show_issue``."""

    async def _make_issue(self, db_session, admin, seed, subject="List comments test"):
        svc = IssueService()
        issue = await svc.create(
            db_session,
            seed["project"],
            IssueCreate(
                project_key="ACME",
                tracker_id=seed["tracker"].id,
                subject=subject,
            ),
            admin,
        )
        await db_session.commit()
        return issue

    async def test_happy_path_desc_default(self, db_session: AsyncSession, admin: User, seed: dict):
        from specivo.mcp.tools import _add_comment, _list_comments

        issue = await self._make_issue(db_session, admin, seed)
        for i in range(3):
            await _add_comment(db_session, admin, issue.display_key, f"comment {i}")
            await db_session.commit()

        result = await _list_comments(db_session, admin, issue.display_key)
        assert "3 total" in result
        assert "comment 0" in result
        assert "comment 1" in result
        assert "comment 2" in result
        # Default order is desc — newest first. "comment 2" should appear before "comment 0".
        assert result.index("comment 2") < result.index("comment 0")

    async def test_pagination(self, db_session: AsyncSession, admin: User, seed: dict):
        from specivo.mcp.tools import _add_comment, _list_comments

        issue = await self._make_issue(db_session, admin, seed)
        for i in range(5):
            await _add_comment(db_session, admin, issue.display_key, f"c{i}")
            await db_session.commit()

        page1 = await _list_comments(db_session, admin, issue.display_key, limit=2, offset=0, order="asc")
        assert "5 total" in page1
        assert "c0" in page1 and "c1" in page1
        assert "c2" not in page1
        assert "offset=2" in page1  # more hint

        page2 = await _list_comments(db_session, admin, issue.display_key, limit=2, offset=2, order="asc")
        assert "c2" in page2 and "c3" in page2
        assert "c0" not in page2

    async def test_asc_ordering(self, db_session: AsyncSession, admin: User, seed: dict):
        from specivo.mcp.tools import _add_comment, _list_comments

        issue = await self._make_issue(db_session, admin, seed)
        await _add_comment(db_session, admin, issue.display_key, "first")
        await db_session.commit()
        await _add_comment(db_session, admin, issue.display_key, "second")
        await db_session.commit()

        result = await _list_comments(db_session, admin, issue.display_key, order="asc")
        assert result.index("first") < result.index("second")

    async def test_show_issue_includes_comments_count(
        self, db_session: AsyncSession, admin: User, seed: dict
    ):
        from specivo.mcp.tools import _add_comment, _show_issue

        issue = await self._make_issue(db_session, admin, seed)
        out_before = await _show_issue(db_session, admin, issue.display_key)
        assert "Comments: 0" in out_before

        await _add_comment(db_session, admin, issue.display_key, "hello")
        await db_session.commit()
        await _add_comment(db_session, admin, issue.display_key, "world")
        await db_session.commit()

        out_after = await _show_issue(db_session, admin, issue.display_key)
        assert "Comments: 2" in out_after

    async def test_empty_case(self, db_session: AsyncSession, admin: User, seed: dict):
        from specivo.mcp.tools import _list_comments

        issue = await self._make_issue(db_session, admin, seed)
        result = await _list_comments(db_session, admin, issue.display_key)
        assert "0 total" in result
        assert "(none)" in result

    async def test_field_change_only_journals_excluded(
        self, db_session: AsyncSession, admin: User, seed: dict, closed_status: IssueStatus
    ):
        """Field-change-only journals (no notes body) must not count as comments."""
        from specivo.mcp.tools import _list_comments, _show_issue
        from specivo.services.journal_service import JournalService

        issue = await self._make_issue(db_session, admin, seed)
        jsvc = JournalService()
        # Simulate a pure field-change (status update, notes=None).
        old_attrs = {"status_id": issue.status_id}
        new_attrs = {"status_id": closed_status.id}
        issue.status_id = closed_status.id
        await jsvc.record_change(db_session, issue, admin, old_attrs, new_attrs, notes=None)
        await db_session.commit()

        out = await _show_issue(db_session, admin, issue.display_key)
        assert "Comments: 0" in out

        result = await _list_comments(db_session, admin, issue.display_key)
        assert "0 total" in result

    async def test_invalid_limit(self, db_session: AsyncSession, admin: User, seed: dict):
        from specivo.core.exceptions import ValidationError
        from specivo.mcp.tools import _list_comments

        issue = await self._make_issue(db_session, admin, seed)
        with pytest.raises(ValidationError):
            await _list_comments(db_session, admin, issue.display_key, limit=0)
        with pytest.raises(ValidationError):
            await _list_comments(db_session, admin, issue.display_key, limit=51)

    async def test_invalid_offset(self, db_session: AsyncSession, admin: User, seed: dict):
        from specivo.core.exceptions import ValidationError
        from specivo.mcp.tools import _list_comments

        issue = await self._make_issue(db_session, admin, seed)
        with pytest.raises(ValidationError):
            await _list_comments(db_session, admin, issue.display_key, offset=-1)

    async def test_invalid_order(self, db_session: AsyncSession, admin: User, seed: dict):
        from specivo.core.exceptions import ValidationError
        from specivo.mcp.tools import _list_comments

        issue = await self._make_issue(db_session, admin, seed)
        with pytest.raises(ValidationError):
            await _list_comments(db_session, admin, issue.display_key, order="sideways")

    async def test_permission_denied_for_non_member_on_private_project(
        self, db_session: AsyncSession, admin: User, seed: dict
    ):
        """Non-admin, non-member user on a private project is denied."""
        from specivo.core.exceptions import NotFoundError, PermissionDeniedError
        from specivo.mcp.tools import _add_comment, _list_comments

        # Private project owned by admin.
        priv = ProjectFactory.build(key="PRIV", identifier="priv-app", is_public=False)
        db_session.add(priv)
        await db_session.commit()
        await db_session.refresh(priv)

        svc = IssueService()
        issue = await svc.create(
            db_session,
            priv,
            IssueCreate(project_key="PRIV", tracker_id=seed["tracker"].id, subject="secret"),
            admin,
        )
        await db_session.commit()
        await _add_comment(db_session, admin, issue.display_key, "top secret")
        await db_session.commit()

        intruder = UserFactory.build(login="intruder", status="active", is_admin=False)
        db_session.add(intruder)
        await db_session.commit()
        await db_session.refresh(intruder)

        # Either PermissionDeniedError (explicit) or NotFoundError (visibility filter
        # hides the issue from non-members) is an acceptable denial — the key
        # contract is that no comment data leaks.
        with pytest.raises((PermissionDeniedError, NotFoundError)):
            await _list_comments(db_session, intruder, issue.display_key)


class TestMcpDeleteVersion:
    async def test_delete_version(self, db_session: AsyncSession, admin: User, project: Project):
        from specivo.mcp.tools import _create_version, _delete_version, _list_versions

        result = await _create_version(db_session, admin, "ACME", "v1.0.0")
        assert "v1.0.0" in result

        # Extract version ID from creation result
        versions_result = await _list_versions(db_session, admin, "ACME")
        assert "v1.0.0" in versions_result

        # Parse version ID from list output (format: "  <id>  [<status>]  <name>")
        version_id: int | None = None
        for line in versions_result.splitlines():
            if "v1.0.0" in line:
                version_id = int(line.strip().split()[0])
                break
        assert version_id is not None

        result = await _delete_version(db_session, admin, "ACME", version_id)
        assert "Deleted" in result
        assert "v1.0.0" in result

        # Verify version is gone
        versions_after = await _list_versions(db_session, admin, "ACME")
        assert "v1.0.0" not in versions_after

    async def test_delete_version_blocked_by_issues(self, db_session: AsyncSession, admin: User, seed: dict):
        from specivo.mcp.tools import _create_issue, _create_version, _delete_version, _list_versions

        # Create a version
        await _create_version(db_session, admin, "ACME", "v2.0.0")
        versions_result = await _list_versions(db_session, admin, "ACME")
        version_id: int | None = None
        for line in versions_result.splitlines():
            if "v2.0.0" in line:
                version_id = int(line.strip().split()[0])
                break
        assert version_id is not None

        # Create an issue linked to this version
        await _create_issue(
            session=db_session,
            user=admin,
            project_key="ACME",
            tracker_id=seed["tracker"].id,
            subject="Linked to version",
            fixed_version_id=version_id,
        )

        # Deletion should be blocked
        result = await _delete_version(db_session, admin, "ACME", version_id)
        assert "Cannot delete" in result
        assert "1 issue(s)" in result

        # Version should still exist
        versions_after = await _list_versions(db_session, admin, "ACME")
        assert "v2.0.0" in versions_after


class TestMcpCreateIssueWithFixedVersion:
    async def test_create_issue_with_fixed_version_via_mcp(self, db_session: AsyncSession, admin: User, seed: dict):
        from specivo.mcp.tools import _create_issue, _create_version, _list_versions

        await _create_version(db_session, admin, "ACME", "v3.0.0")
        versions_result = await _list_versions(db_session, admin, "ACME")
        version_id: int | None = None
        for line in versions_result.splitlines():
            if "v3.0.0" in line:
                version_id = int(line.strip().split()[0])
                break
        assert version_id is not None

        result = await _create_issue(
            session=db_session,
            user=admin,
            project_key="ACME",
            tracker_id=seed["tracker"].id,
            subject="Issue with version",
            fixed_version_id=version_id,
        )
        assert "ACME-1" in result
        assert "Issue with version" in result

        # Verify the issue is actually linked to the version
        svc = IssueService()
        issue = await svc.get_by_display_key(db_session, "ACME-1", user=admin)
        assert issue.fixed_version_id == version_id


class TestMcpUpdateIssueFixedVersion:
    async def test_update_issue_fixed_version_via_mcp(self, db_session: AsyncSession, admin: User, seed: dict):
        from specivo.mcp.tools import _create_issue, _create_version, _list_versions, _update_issue

        # Create two versions
        await _create_version(db_session, admin, "ACME", "v4.0.0")
        await _create_version(db_session, admin, "ACME", "v5.0.0")
        versions_result = await _list_versions(db_session, admin, "ACME")

        version_ids: dict[str, int] = {}
        for line in versions_result.splitlines():
            for vname in ("v4.0.0", "v5.0.0"):
                if vname in line:
                    version_ids[vname] = int(line.strip().split()[0])
        assert len(version_ids) == 2

        # Create issue without version
        await _create_issue(
            session=db_session,
            user=admin,
            project_key="ACME",
            tracker_id=seed["tracker"].id,
            subject="Reassign version test",
        )

        # Assign to v4
        result = await _update_issue(db_session, admin, "ACME-1", fixed_version_id=version_ids["v4.0.0"])
        assert "ACME-1" in result

        svc = IssueService()
        issue = await svc.get_by_display_key(db_session, "ACME-1", user=admin)
        assert issue.fixed_version_id == version_ids["v4.0.0"]

        # Reassign to v5
        result = await _update_issue(db_session, admin, "ACME-1", fixed_version_id=version_ids["v5.0.0"])
        assert "ACME-1" in result

        await db_session.refresh(issue)
        assert issue.fixed_version_id == version_ids["v5.0.0"]


class TestMcpUpdateIssuePreservesUnrelatedFields:
    """Regression tests: omitted fields must not wipe existing sprint/version.

    Pydantic V2 adds every kwarg to ``model_fields_set`` regardless of value,
    and ``IssueService.update`` uses that set to distinguish "not provided"
    from "explicitly cleared" for ``fixed_version_id`` and ``sprint_id``.
    The MCP wrapper must only forward kwargs the caller actually supplied.
    """

    async def test_update_preserves_version_and_sprint_when_omitted(
        self, db_session: AsyncSession, admin: User, seed: dict
    ):
        from specivo.mcp.tools import (
            _create_issue,
            _create_version,
            _list_versions,
            _update_issue,
        )

        # Create version
        await _create_version(db_session, admin, "ACME", "v6.0.0")
        versions_result = await _list_versions(db_session, admin, "ACME")
        version_id: int | None = None
        for line in versions_result.splitlines():
            if "v6.0.0" in line:
                version_id = int(line.strip().split()[0])
                break
        assert version_id is not None

        # Create sprint directly via service to get its id
        from specivo.schemas.sprint import SprintCreate
        from specivo.services.sprint_service import SprintService
        sprint_svc = SprintService()
        sprint = await sprint_svc.create(
            db_session, seed["project"], SprintCreate(name="Sprint A")
        )
        await db_session.commit()
        sprint_id = sprint.id

        # Create issue with both version and sprint assigned
        await _create_issue(
            session=db_session,
            user=admin,
            project_key="ACME",
            tracker_id=seed["tracker"].id,
            subject="Has version and sprint",
            fixed_version_id=version_id,
            sprint_id=sprint_id,
        )
        await db_session.commit()

        svc = IssueService()
        issue = await svc.get_by_display_key(db_session, "ACME-1", user=admin)
        assert issue.fixed_version_id == version_id
        assert issue.sprint_id == sprint_id

        # Update unrelated fields only — version and sprint MUST be preserved
        await _update_issue(
            db_session,
            admin,
            "ACME-1",
            status_id=seed["status"].id,
            done_ratio=50,
            notes="progress update",
        )
        await db_session.commit()

        await db_session.refresh(issue)
        assert issue.fixed_version_id == version_id, (
            "fixed_version_id was wiped by an update that did not mention it"
        )
        assert issue.sprint_id == sprint_id, (
            "sprint_id was wiped by an update that did not mention it"
        )
        assert issue.done_ratio == 50

    async def test_update_can_still_set_version_and_sprint_explicitly(
        self, db_session: AsyncSession, admin: User, seed: dict
    ):
        from specivo.mcp.tools import (
            _create_issue,
            _create_version,
            _list_versions,
            _update_issue,
        )
        from specivo.schemas.sprint import SprintCreate
        from specivo.services.sprint_service import SprintService

        await _create_version(db_session, admin, "ACME", "v7.0.0")
        versions_result = await _list_versions(db_session, admin, "ACME")
        new_version_id: int | None = None
        for line in versions_result.splitlines():
            if "v7.0.0" in line:
                new_version_id = int(line.strip().split()[0])
                break
        assert new_version_id is not None

        sprint_svc = SprintService()
        new_sprint = await sprint_svc.create(
            db_session, seed["project"], SprintCreate(name="Sprint B")
        )
        await db_session.commit()

        await _create_issue(
            session=db_session,
            user=admin,
            project_key="ACME",
            tracker_id=seed["tracker"].id,
            subject="Will receive version and sprint",
        )
        await db_session.commit()

        await _update_issue(
            db_session,
            admin,
            "ACME-1",
            fixed_version_id=new_version_id,
            sprint_id=new_sprint.id,
        )
        await db_session.commit()

        svc = IssueService()
        issue = await svc.get_by_display_key(db_session, "ACME-1", user=admin)
        assert issue.fixed_version_id == new_version_id
        assert issue.sprint_id == new_sprint.id

    async def test_edit_description_preserves_version_and_sprint(
        self, db_session: AsyncSession, admin: User, seed: dict
    ):
        from specivo.mcp.tools import (
            _create_issue,
            _create_version,
            _edit_description,
            _list_versions,
        )
        from specivo.schemas.sprint import SprintCreate
        from specivo.services.sprint_service import SprintService

        await _create_version(db_session, admin, "ACME", "v8.0.0")
        versions_result = await _list_versions(db_session, admin, "ACME")
        version_id: int | None = None
        for line in versions_result.splitlines():
            if "v8.0.0" in line:
                version_id = int(line.strip().split()[0])
                break
        assert version_id is not None

        sprint_svc = SprintService()
        sprint = await sprint_svc.create(
            db_session, seed["project"], SprintCreate(name="Sprint C")
        )
        await db_session.commit()

        await _create_issue(
            session=db_session,
            user=admin,
            project_key="ACME",
            tracker_id=seed["tracker"].id,
            subject="Edit desc preserves version",
            description="original body text",
            fixed_version_id=version_id,
            sprint_id=sprint.id,
        )
        await db_session.commit()

        await _edit_description(db_session, admin, "ACME-1", "original", "replaced")
        await db_session.commit()

        svc = IssueService()
        issue = await svc.get_by_display_key(db_session, "ACME-1", user=admin)
        assert "replaced" in (issue.description or "")
        assert issue.fixed_version_id == version_id, (
            "edit_description wiped fixed_version_id"
        )
        assert issue.sprint_id == sprint.id, "edit_description wiped sprint_id"
