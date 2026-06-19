"""Integration tests for the Tags MCP tools.

Covers the vocabulary tools (_list_tags, _create_tag, _update_tag, _delete_tag)
and the unified entity tool (_tag) over issues and wiki pages, plus permission
enforcement and audit logging.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import PermissionDeniedError
from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.role import Role
from specivo.models.security_audit import SecurityAuditLog
from specivo.models.user import User
from specivo.schemas.issue import IssueCreate
from specivo.services.issue_service import IssueService
from specivo.services.wiki_service import WikiService
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.serial]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
    proj = ProjectFactory.build(key="MTAG", name="MCP Tag Test", is_public=True)
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


@pytest_asyncio.fixture
async def admin(db_session: AsyncSession) -> User:
    user = AdminUserFactory.build(login="mtag_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def viewer(db_session: AsyncSession) -> User:
    user = UserFactory.build(login="mtag_viewer", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def role_view_only(db_session: AsyncSession) -> Role:
    result = await db_session.execute(select(Role).where(Role.name == "MTagViewOnly"))
    existing = result.scalar_one_or_none()
    if existing:
        return existing
    role = Role(
        name="MTagViewOnly",
        position=13,
        permissions=["view_issues"],
        issues_visibility="default",
        builtin=0,
    )
    db_session.add(role)
    await db_session.commit()
    await db_session.refresh(role)
    return role


@pytest_asyncio.fixture
async def issue(db_session, project, tracker, priority, status, admin):
    svc = IssueService()
    created = await svc.create(
        db_session,
        project,
        IssueCreate(project_key=project.key, tracker_id=tracker.id, subject="MCP tag"),
        admin,
    )
    await db_session.commit()
    await db_session.refresh(created)
    return created


@pytest_asyncio.fixture
async def wiki_page(db_session, project, admin):
    page, _ = await WikiService().create_page(
        db_session, project.id, title="MCP Wiki Tag", text="body", author=admin
    )
    await db_session.commit()
    await db_session.refresh(page)
    return page


async def _add_member(db, project, user, role):
    member = Member(user_id=user.id, project_id=project.id)
    db.add(member)
    await db.flush()
    db.add(MemberRole(member_id=member.id, role_id=role.id))
    await db.commit()


# ---------------------------------------------------------------------------
# Vocabulary tools
# ---------------------------------------------------------------------------


class TestTagVocabularyTools:
    async def test_create_list_update_delete(self, db_session, admin, project):
        from specivo.mcp.tools import _create_tag, _delete_tag, _list_tags, _update_tag

        result = await _create_tag(db_session, admin, project.key, "backend", "#4f9d6c")
        assert "Created tag 'backend'" in result

        listing = await _list_tags(db_session, admin, project.key)
        assert "backend" in listing
        assert "#4f9d6c" in listing

        # Resolve the tag id from the service to update it.
        from specivo.services.tag_service import TagService

        tag = await TagService().get_by_name(db_session, project.id, "backend")
        assert tag is not None

        upd = await _update_tag(db_session, admin, project.key, tag.id, "core", None)
        assert "Updated tag 'core'" in upd

        deleted = await _delete_tag(db_session, admin, project.key, tag.id)
        assert "Deleted tag 'core'" in deleted

    async def test_create_duplicate_returns_error(self, db_session, admin, project):
        from specivo.mcp.tools import _create_tag

        await _create_tag(db_session, admin, project.key, "dup", None)
        result = await _create_tag(db_session, admin, project.key, "DUP", None)
        assert result.startswith("Error:")

    async def test_create_requires_manage_project(self, db_session, viewer, project, role_view_only):
        from specivo.mcp.tools import _create_tag

        await _add_member(db_session, project, viewer, role_view_only)
        with pytest.raises(PermissionDeniedError):
            await _create_tag(db_session, viewer, project.key, "nope", None)


# ---------------------------------------------------------------------------
# Entity tool: _tag
# ---------------------------------------------------------------------------


class TestTagEntityTool:
    async def test_add_get_remove_on_issue(self, db_session, admin, issue):
        from specivo.mcp.tools import _tag

        added = await _tag(db_session, admin, issue.display_key, "add", "urgent")
        assert "urgent" in added

        got = await _tag(db_session, admin, issue.display_key, "get")
        assert "urgent" in got

        removed = await _tag(db_session, admin, issue.display_key, "remove", "urgent")
        assert "urgent" in removed

        got = await _tag(db_session, admin, issue.display_key, "get")
        assert "(no tags)" in got

    async def test_set_replaces_full_list(self, db_session, admin, issue):
        from specivo.mcp.tools import _tag

        await _tag(db_session, admin, issue.display_key, "set", ["a", "b"])
        result = await _tag(db_session, admin, issue.display_key, "set", ["b", "c"])
        assert "added" in result and "removed" in result
        got = await _tag(db_session, admin, issue.display_key, "get")
        assert "b" in got and "c" in got and "a" not in got.split(": ", 1)[1].split(", ")

    async def test_add_on_wiki(self, db_session, admin, project, wiki_page):
        from specivo.mcp.tools import _tag

        ref = f"wiki:{project.key}/{wiki_page.slug}"
        added = await _tag(db_session, admin, ref, "add", "docs")
        assert "docs" in added
        got = await _tag(db_session, admin, ref, "get")
        assert "docs" in got

    async def test_invalid_op_returns_error(self, db_session, admin, issue):
        from specivo.mcp.tools import _tag

        result = await _tag(db_session, admin, issue.display_key, "frobnicate", "x")
        assert result.startswith("Error:")

    async def test_add_writes_audit_log(self, db_session, admin, issue):
        from specivo.mcp.tools import _tag

        await _tag(db_session, admin, issue.display_key, "add", "tracked")
        rows = (
            await db_session.execute(
                select(SecurityAuditLog).where(SecurityAuditLog.event_type == "tag_added")
            )
        ).scalars().all()
        assert len(rows) >= 1
        assert rows[0].details.get("source") == "mcp"
