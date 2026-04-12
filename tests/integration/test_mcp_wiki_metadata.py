"""Integration tests for MCP wiki metadata update tool."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import NotFoundError
from specivo.models.project import EnabledModule, Project
from specivo.models.user import User
from specivo.models.wiki import WikiRedirect
from specivo.services.wiki_service import WikiService
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.serial]

_wiki_svc = WikiService()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def project(db_session: AsyncSession) -> Project:
    proj = ProjectFactory.build(key="MWIK", name="MCP Wiki Meta Test", is_public=True)
    db_session.add(proj)
    await db_session.flush()
    db_session.add(EnabledModule(project_id=proj.id, name="wiki"))
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


@pytest_asyncio.fixture
async def admin_user(db_session: AsyncSession) -> User:
    user = AdminUserFactory.build(login="mcp_wmeta_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def wiki_page(db_session: AsyncSession, project: Project, admin_user: User):
    """Create a wiki page for testing."""
    page, content = await _wiki_svc.create_page(
        db_session, project.id, "Test Page", "Some content", admin_user,
    )
    await db_session.commit()
    await db_session.refresh(page)
    return page


@pytest_asyncio.fixture
async def child_page(db_session: AsyncSession, project: Project, admin_user: User, wiki_page):
    """Create a child wiki page under wiki_page."""
    page, content = await _wiki_svc.create_page(
        db_session, project.id, "Child Page", "Child content", admin_user,
        parent_slug=wiki_page.slug,
    )
    await db_session.commit()
    await db_session.refresh(page)
    return page


@pytest_asyncio.fixture
async def parent_page(db_session: AsyncSession, project: Project, admin_user: User):
    """Create a separate parent page."""
    page, content = await _wiki_svc.create_page(
        db_session, project.id, "New Parent", "Parent content", admin_user,
    )
    await db_session.commit()
    await db_session.refresh(page)
    return page


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestUpdateWikiMetadataReparent:
    async def test_update_wiki_metadata_reparent(
        self, db_session, admin_user, project, wiki_page, parent_page,
    ):
        from specivo.mcp.tools import _update_wiki_metadata

        result = await _update_wiki_metadata(
            db_session, admin_user, project.key, wiki_page.slug,
            parent_slug=parent_page.slug,
        )
        assert "parent" in result
        assert parent_page.slug in result

        await db_session.refresh(wiki_page)
        assert wiki_page.parent_id == parent_page.id


class TestUpdateWikiMetadataMakeRoot:
    async def test_update_wiki_metadata_make_root(
        self, db_session, admin_user, project, child_page, wiki_page,
    ):
        from specivo.mcp.tools import _update_wiki_metadata

        # Verify child_page has a parent
        assert child_page.parent_id == wiki_page.id

        result = await _update_wiki_metadata(
            db_session, admin_user, project.key, child_page.slug,
            parent_slug="",
        )
        assert "root" in result

        await db_session.refresh(child_page)
        assert child_page.parent_id is None


class TestUpdateWikiMetadataRename:
    async def test_update_wiki_metadata_rename(
        self, db_session, admin_user, project, wiki_page,
    ):
        from specivo.mcp.tools import _update_wiki_metadata

        old_slug = wiki_page.slug
        result = await _update_wiki_metadata(
            db_session, admin_user, project.key, old_slug,
            title="Renamed Page",
        )
        assert "title" in result
        assert "Renamed Page" in result
        assert "renamed-page" in result

        await db_session.refresh(wiki_page)
        assert wiki_page.title == "Renamed Page"
        assert wiki_page.slug == "renamed-page"

        # Verify redirect was created
        from sqlalchemy import select

        stmt = select(WikiRedirect).where(
            WikiRedirect.wiki_id == wiki_page.wiki_id,
            WikiRedirect.title_from == old_slug,
        )
        redir = (await db_session.execute(stmt)).scalar_one_or_none()
        assert redir is not None
        assert redir.redirected_to == "renamed-page"


class TestUpdateWikiMetadataSetProtected:
    async def test_update_wiki_metadata_set_protected(
        self, db_session, admin_user, project, wiki_page,
    ):
        from specivo.mcp.tools import _update_wiki_metadata

        assert wiki_page.protected is False

        result = await _update_wiki_metadata(
            db_session, admin_user, project.key, wiki_page.slug,
            protected=True,
        )
        assert "protected" in result
        assert "True" in result

        await db_session.refresh(wiki_page)
        assert wiki_page.protected is True


class TestUpdateWikiMetadataNoChangesError:
    async def test_update_wiki_metadata_no_changes_error(
        self, db_session, admin_user, project, wiki_page,
    ):
        from specivo.mcp.tools import _update_wiki_metadata

        result = await _update_wiki_metadata(
            db_session, admin_user, project.key, wiki_page.slug,
        )
        assert "Error" in result
        assert "at least one" in result


class TestUpdateWikiMetadataNonexistentPage:
    async def test_update_wiki_metadata_nonexistent_page(
        self, db_session, admin_user, project,
    ):
        from specivo.mcp.tools import _update_wiki_metadata

        with pytest.raises(NotFoundError):
            await _update_wiki_metadata(
                db_session, admin_user, project.key, "nonexistent-slug",
                title="New Title",
            )
