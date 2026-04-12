"""Integration tests for MCP issue relation tools."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import PermissionDeniedError
from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.role import Role
from specivo.models.user import User
from specivo.schemas.issue import IssueCreate
from specivo.services.issue_service import IssueService
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.serial]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _add_member(db: AsyncSession, project: Project, user: User, role: Role) -> Member:
    member = Member(user_id=user.id, project_id=project.id)
    db.add(member)
    await db.flush()
    mr = MemberRole(member_id=member.id, role_id=role.id)
    db.add(mr)
    await db.commit()
    await db.refresh(member)
    return member


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def status(db_session: AsyncSession) -> IssueStatus:
    s = StatusFactory.build(name="New", position=1, category="backlog")
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
    proj = ProjectFactory.build(key="MREL", name="MCP Relations Test", is_public=True)
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


@pytest_asyncio.fixture
async def admin_user(db_session: AsyncSession) -> User:
    user = AdminUserFactory.build(login="mcp_rel_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def limited_user(db_session: AsyncSession) -> User:
    user = UserFactory.build(login="mcp_rel_limited", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def role_view_only(db_session: AsyncSession) -> Role:
    result = await db_session.execute(select(Role).where(Role.name == "RelViewOnly"))
    existing = result.scalar_one_or_none()
    if existing:
        return existing
    role = Role(
        name="RelViewOnly",
        position=10,
        permissions=["view_issues"],
        issues_visibility="default",
        builtin=0,
    )
    db_session.add(role)
    await db_session.commit()
    await db_session.refresh(role)
    return role


@pytest_asyncio.fixture
async def issues(
    db_session: AsyncSession,
    project: Project,
    tracker: Tracker,
    priority: IssuePriority,
    admin_user: User,
) -> tuple:
    """Create two issues for relation testing."""
    svc = IssueService()
    issue1 = await svc.create(
        db_session,
        project,
        IssueCreate(project_key=project.key, tracker_id=tracker.id, subject="Issue A"),
        admin_user,
    )
    issue2 = await svc.create(
        db_session,
        project,
        IssueCreate(project_key=project.key, tracker_id=tracker.id, subject="Issue B"),
        admin_user,
    )
    await db_session.commit()
    return issue1, issue2


# ---------------------------------------------------------------------------
# Tests: list relations
# ---------------------------------------------------------------------------


class TestMcpListRelations:
    async def test_list_empty(self, db_session, admin_user, issues):
        from specivo.mcp.tools import _list_relations

        issue1, _ = issues
        result = await _list_relations(db_session, admin_user, issue1.display_key)
        assert "No relations" in result

    async def test_list_after_add(self, db_session, admin_user, issues):
        from specivo.mcp.tools import _add_relation, _list_relations

        issue1, issue2 = issues
        await _add_relation(db_session, admin_user, issue1.display_key, issue2.display_key, "blocks")
        result = await _list_relations(db_session, admin_user, issue1.display_key)
        assert "blocks" in result
        assert issue2.display_key in result


# ---------------------------------------------------------------------------
# Tests: add relation
# ---------------------------------------------------------------------------


class TestMcpAddRelation:
    async def test_add_blocks(self, db_session, admin_user, issues):
        from specivo.mcp.tools import _add_relation

        issue1, issue2 = issues
        result = await _add_relation(db_session, admin_user, issue1.display_key, issue2.display_key, "blocks")
        assert "blocks" in result.lower()
        assert issue1.display_key in result
        assert issue2.display_key in result

    async def test_add_relates(self, db_session, admin_user, issues):
        from specivo.mcp.tools import _add_relation

        issue1, issue2 = issues
        result = await _add_relation(db_session, admin_user, issue1.display_key, issue2.display_key, "relates")
        assert "relates" in result.lower()

    async def test_add_invalid_type(self, db_session, admin_user, issues):
        from specivo.mcp.tools import _add_relation

        issue1, issue2 = issues
        result = await _add_relation(db_session, admin_user, issue1.display_key, issue2.display_key, "invalid_type")
        assert "invalid" in result.lower() or "error" in result.lower()

    async def test_add_denied_without_permission(
        self,
        db_session,
        limited_user,
        role_view_only,
        project,
        issues,
    ):
        from specivo.mcp.tools import _add_relation

        await _add_member(db_session, project, limited_user, role_view_only)
        issue1, issue2 = issues
        with pytest.raises(PermissionDeniedError, match="manage_issue_relations"):
            await _add_relation(db_session, limited_user, issue1.display_key, issue2.display_key, "blocks")


# ---------------------------------------------------------------------------
# Tests: remove relation
# ---------------------------------------------------------------------------


class TestMcpRemoveRelation:
    async def test_remove_relation(self, db_session, admin_user, issues):
        from specivo.mcp.tools import _add_relation, _list_relations, _remove_relation

        issue1, issue2 = issues
        add_result = await _add_relation(db_session, admin_user, issue1.display_key, issue2.display_key, "blocks")
        # Extract relation ID from result
        # Result format: "Created relation #<id>: ..."
        import re

        match = re.search(r"#(\d+)", add_result)
        assert match, f"Could not find relation ID in: {add_result}"
        rel_id = int(match.group(1))

        result = await _remove_relation(db_session, admin_user, issue1.display_key, rel_id)
        assert "deleted" in result.lower() or "removed" in result.lower()

        # Verify it's gone
        list_result = await _list_relations(db_session, admin_user, issue1.display_key)
        assert "No relations" in list_result

    async def test_remove_denied_without_permission(
        self,
        db_session,
        admin_user,
        limited_user,
        role_view_only,
        project,
        issues,
    ):
        from specivo.mcp.tools import _add_relation, _remove_relation

        await _add_member(db_session, project, limited_user, role_view_only)
        issue1, issue2 = issues
        add_result = await _add_relation(db_session, admin_user, issue1.display_key, issue2.display_key, "blocks")
        import re

        rel_id = int(re.search(r"#(\d+)", add_result).group(1))

        with pytest.raises(PermissionDeniedError, match="manage_issue_relations"):
            await _remove_relation(db_session, limited_user, issue1.display_key, rel_id)
