"""TDD red-phase tests for MCP tool permission enforcement.

These tests FAIL until ``_require_permission`` is implemented in
``specivo/mcp/tools.py`` and wired into each mutating tool function.

The contract under test:
- Every write/manage tool checks the caller's project-scoped permissions.
- ``PermissionDeniedError`` is raised when the user lacks the required
  permission — NOT after the write is attempted.
- Admin users bypass all checks (``user.is_admin == True``).
- Non-members on private projects are denied access to all tools,
  including read-only ones that would otherwise expose private data.

Tool-to-permission mapping tested here:
  _create_issue   -> add_issues
  _update_issue   -> edit_issues
  _add_comment    -> add_issue_notes
  _create_wiki    -> manage_wiki
  _edit_wiki      -> manage_wiki
  _log_time       -> log_time
  _create_version -> manage_versions
  _read_wiki      -> view_wiki  (positive control: viewer role has it)
  _list_issues    -> view_issues on private project (non-member denied)
"""

from __future__ import annotations

from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import PermissionDeniedError
from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.role import Role
from specivo.models.time_entry import TimeEntryActivity
from specivo.models.user import User
from specivo.schemas.issue import IssueCreate
from specivo.services.issue_service import IssueService
from specivo.services.wiki_service import WikiService
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.serial]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _add_member(
    db_session: AsyncSession,
    project: Project,
    user: User,
    role: Role,
) -> Member:
    """Add *user* as a project member with *role*."""
    member = Member(user_id=user.id, project_id=project.id)
    db_session.add(member)
    await db_session.flush()
    mr = MemberRole(member_id=member.id, role_id=role.id)
    db_session.add(mr)
    await db_session.commit()
    await db_session.refresh(member)
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
async def activity(db_session: AsyncSession) -> TimeEntryActivity:
    # Use SELECT-or-INSERT pattern to avoid unique constraint collisions when
    # another test in the same DB session has already inserted this activity.
    result = await db_session.execute(select(TimeEntryActivity).where(TimeEntryActivity.name == "MCP Perm Dev"))
    existing = result.scalar_one_or_none()
    if existing:
        return existing
    a = TimeEntryActivity(name="MCP Perm Dev", position=99, is_default=False, active=True)
    db_session.add(a)
    await db_session.commit()
    await db_session.refresh(a)
    return a


@pytest_asyncio.fixture
async def public_project(db_session: AsyncSession) -> Project:
    """Public project used as the default test project."""
    proj = ProjectFactory.build(key="MPUB", name="MCP Permission Public", is_public=True)
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


@pytest_asyncio.fixture
async def private_project(db_session: AsyncSession) -> Project:
    """Private project — non-members cannot access any resource here."""
    proj = ProjectFactory.build(key="MPRV", name="MCP Permission Private", is_public=False)
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


@pytest_asyncio.fixture
async def admin_user(db_session: AsyncSession) -> User:
    user = AdminUserFactory.build(login="mcp_perm_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def limited_user(db_session: AsyncSession) -> User:
    """Regular user — NOT yet added to any project; roles are applied per test."""
    user = UserFactory.build(login="mcp_limited_user", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def non_member_user(db_session: AsyncSession) -> User:
    """User who is never added to any project."""
    user = UserFactory.build(login="mcp_non_member", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def role_view_issues_only(db_session: AsyncSession) -> Role:
    """Role granting only ``view_issues`` and ``view_wiki`` — no write permissions."""
    result = await db_session.execute(select(Role).where(Role.name == "ViewIssuesOnly"))
    existing = result.scalar_one_or_none()
    if existing:
        return existing
    role = Role(
        name="ViewIssuesOnly",
        position=10,
        permissions=["view_issues", "view_wiki"],
        issues_visibility="default",
        builtin=0,
    )
    db_session.add(role)
    await db_session.commit()
    await db_session.refresh(role)
    return role


@pytest_asyncio.fixture
async def role_full_access(db_session: AsyncSession) -> Role:
    """Role granting all permissions (``"*"``) — for positive-control tests."""
    result = await db_session.execute(select(Role).where(Role.name == "FullAccess"))
    existing = result.scalar_one_or_none()
    if existing:
        return existing
    role = Role(
        name="FullAccess",
        position=1,
        permissions=["*"],
        issues_visibility="all",
        builtin=0,
    )
    db_session.add(role)
    await db_session.commit()
    await db_session.refresh(role)
    return role


@pytest_asyncio.fixture
async def seed(status, tracker, priority, public_project, activity):
    """Bundle of all lookups needed for issue creation."""
    return {
        "status": status,
        "tracker": tracker,
        "priority": priority,
        "project": public_project,
        "activity": activity,
    }


@pytest_asyncio.fixture
async def existing_issue(db_session: AsyncSession, seed: dict, admin_user: User):
    """An issue on the public project, created by admin so it always exists."""
    svc = IssueService()
    issue = await svc.create(
        db_session,
        seed["project"],
        IssueCreate(
            project_key=seed["project"].key,
            tracker_id=seed["tracker"].id,
            subject="Pre-existing issue for permission tests",
        ),
        admin_user,
    )
    await db_session.commit()
    return issue


@pytest_asyncio.fixture
async def existing_wiki_page(db_session: AsyncSession, public_project: Project, admin_user: User):
    """A wiki page on the public project created by admin."""
    wiki_svc = WikiService()
    page, _ = await wiki_svc.create_page(
        db_session, public_project.id, "Permission Test Page", "Initial content.", admin_user
    )
    await db_session.commit()
    return page


# ---------------------------------------------------------------------------
# Tests: denied when lacking specific permission
# ---------------------------------------------------------------------------


class TestMcpPermissionDenied:
    """Each write tool raises PermissionDeniedError when the user's role
    does not include the required permission.

    All tests in this class use ``limited_user`` who is a member of
    ``public_project`` with ``role_view_issues_only`` (only ``view_issues``
    and ``view_wiki``).
    """

    async def test_create_issue_denied_without_add_issues(
        self,
        db_session: AsyncSession,
        limited_user: User,
        public_project: Project,
        role_view_issues_only: Role,
        seed: dict,
    ):
        """_create_issue raises PermissionDeniedError when user lacks add_issues."""
        from specivo.mcp.tools import _create_issue

        await _add_member(db_session, public_project, limited_user, role_view_issues_only)

        with pytest.raises(PermissionDeniedError, match="add_issues"):
            await _create_issue(
                session=db_session,
                user=limited_user,
                project_key=public_project.key,
                tracker_id=seed["tracker"].id,
                subject="Should be denied",
            )

    async def test_update_issue_denied_without_edit_issues(
        self,
        db_session: AsyncSession,
        limited_user: User,
        public_project: Project,
        role_view_issues_only: Role,
        existing_issue,
    ):
        """_update_issue raises PermissionDeniedError when user lacks edit_issues."""
        from specivo.mcp.tools import _update_issue

        await _add_member(db_session, public_project, limited_user, role_view_issues_only)

        with pytest.raises(PermissionDeniedError, match="edit_issues"):
            await _update_issue(
                session=db_session,
                user=limited_user,
                issue_ref=existing_issue.display_key,
                subject="Should be denied",
            )

    async def test_add_comment_denied_without_add_issue_notes(
        self,
        db_session: AsyncSession,
        limited_user: User,
        public_project: Project,
        role_view_issues_only: Role,
        existing_issue,
    ):
        """_add_comment raises PermissionDeniedError when user lacks add_issue_notes."""
        from specivo.mcp.tools import _add_comment

        await _add_member(db_session, public_project, limited_user, role_view_issues_only)

        with pytest.raises(PermissionDeniedError, match="add_issue_notes"):
            await _add_comment(
                session=db_session,
                user=limited_user,
                issue_ref=existing_issue.display_key,
                notes="This comment should be denied",
            )

    async def test_create_wiki_denied_without_manage_wiki(
        self,
        db_session: AsyncSession,
        limited_user: User,
        public_project: Project,
        role_view_issues_only: Role,
    ):
        """_create_wiki raises PermissionDeniedError when user lacks manage_wiki."""
        from specivo.mcp.tools import _create_wiki

        await _add_member(db_session, public_project, limited_user, role_view_issues_only)

        with pytest.raises(PermissionDeniedError, match="manage_wiki"):
            await _create_wiki(
                session=db_session,
                user=limited_user,
                project_key=public_project.key,
                title="Should Not Exist",
                text="Denied content",
            )

    async def test_edit_wiki_denied_without_manage_wiki(
        self,
        db_session: AsyncSession,
        limited_user: User,
        public_project: Project,
        role_view_issues_only: Role,
        existing_wiki_page,
    ):
        """_edit_wiki raises PermissionDeniedError when user lacks manage_wiki."""
        from specivo.mcp.tools import _edit_wiki

        await _add_member(db_session, public_project, limited_user, role_view_issues_only)

        with pytest.raises(PermissionDeniedError, match="manage_wiki"):
            await _edit_wiki(
                session=db_session,
                user=limited_user,
                project_key=public_project.key,
                slug=existing_wiki_page.slug,
                search_text="Initial content",
                replace_text="Denied replacement",
            )

    async def test_log_time_denied_without_log_time_permission(
        self,
        db_session: AsyncSession,
        limited_user: User,
        public_project: Project,
        role_view_issues_only: Role,
        seed: dict,
    ):
        """_log_time raises PermissionDeniedError when user lacks log_time."""
        from specivo.mcp.tools import _log_time

        await _add_member(db_session, public_project, limited_user, role_view_issues_only)

        with pytest.raises(PermissionDeniedError, match="log_time"):
            await _log_time(
                session=db_session,
                user=limited_user,
                project_key=public_project.key,
                hours=Decimal("1.5"),
                activity_id=seed["activity"].id,
            )

    async def test_create_version_denied_without_manage_versions(
        self,
        db_session: AsyncSession,
        limited_user: User,
        public_project: Project,
        role_view_issues_only: Role,
    ):
        """_create_version raises PermissionDeniedError when user lacks manage_versions."""
        from specivo.mcp.tools import _create_version

        await _add_member(db_session, public_project, limited_user, role_view_issues_only)

        with pytest.raises(PermissionDeniedError, match="manage_versions"):
            await _create_version(
                session=db_session,
                user=limited_user,
                project_key=public_project.key,
                name="v1.0-denied",
            )


# ---------------------------------------------------------------------------
# Tests: positive controls (permission granted)
# ---------------------------------------------------------------------------


class TestMcpPermissionGranted:
    """Verify that tools succeed when the user actually holds the required
    permission.  These tests confirm the ``_require_permission`` guard does
    not block legitimate access.
    """

    async def test_read_wiki_succeeds_with_view_wiki(
        self,
        db_session: AsyncSession,
        limited_user: User,
        public_project: Project,
        role_view_issues_only: Role,
        existing_wiki_page,
    ):
        """_read_wiki does NOT raise when user has view_wiki permission."""
        from specivo.mcp.tools import _read_wiki

        # role_view_issues_only includes "view_wiki"
        await _add_member(db_session, public_project, limited_user, role_view_issues_only)

        result = await _read_wiki(
            session=db_session,
            user=limited_user,
            project_key=public_project.key,
            slug=existing_wiki_page.slug,
        )
        assert existing_wiki_page.title in result

    async def test_create_issue_succeeds_with_add_issues(
        self,
        db_session: AsyncSession,
        limited_user: User,
        public_project: Project,
        role_full_access: Role,
        seed: dict,
    ):
        """_create_issue does NOT raise when user has add_issues permission."""
        from specivo.mcp.tools import _create_issue

        # role_full_access grants "*" (all permissions)
        await _add_member(db_session, public_project, limited_user, role_full_access)

        result = await _create_issue(
            session=db_session,
            user=limited_user,
            project_key=public_project.key,
            tracker_id=seed["tracker"].id,
            subject="Permitted issue creation",
        )
        assert "Permitted issue creation" in result
        assert public_project.key in result


# ---------------------------------------------------------------------------
# Tests: non-member on private project
# ---------------------------------------------------------------------------


class TestMcpNonMemberPrivateProject:
    """A user who is not a member of a private project must be denied for
    every tool, because ``check_permission`` returns False for non-members.
    """

    async def test_list_issues_denied_for_non_member_on_private_project(
        self,
        db_session: AsyncSession,
        non_member_user: User,
        private_project: Project,
        seed: dict,
        admin_user: User,
    ):
        """_list_issues raises PermissionDeniedError for non-member on private project.

        An issue is seeded by admin first so the project isn't empty; the
        non-member should still be denied before any data is returned.
        """
        from specivo.mcp.tools import _list_issues

        # Seed an issue via admin so there is something to protect
        svc = IssueService()
        await svc.create(
            db_session,
            private_project,
            IssueCreate(
                project_key=private_project.key,
                tracker_id=seed["tracker"].id,
                subject="Private issue non-member cannot see",
            ),
            admin_user,
        )
        await db_session.commit()

        with pytest.raises(PermissionDeniedError):
            await _list_issues(
                session=db_session,
                user=non_member_user,
                project_key=private_project.key,
            )


# ---------------------------------------------------------------------------
# Tests: admin bypasses all permission checks
# ---------------------------------------------------------------------------


class TestMcpAdminBypass:
    """Admin users have ``is_admin=True``.  ``check_permission`` short-circuits
    to ``True`` for them, so no PermissionDeniedError is ever raised regardless
    of project membership or role configuration.
    """

    async def test_admin_can_create_issue_without_membership(
        self,
        db_session: AsyncSession,
        admin_user: User,
        public_project: Project,
        seed: dict,
    ):
        """Admin creates issue with no Member row — check_permission bypassed."""
        from specivo.mcp.tools import _create_issue

        result = await _create_issue(
            session=db_session,
            user=admin_user,
            project_key=public_project.key,
            tracker_id=seed["tracker"].id,
            subject="Admin bypass test",
        )
        assert "Admin bypass test" in result

    async def test_admin_can_create_wiki_without_membership(
        self,
        db_session: AsyncSession,
        admin_user: User,
        public_project: Project,
    ):
        """Admin creates a wiki page with no Member row — check_permission bypassed."""
        from specivo.mcp.tools import _create_wiki

        result = await _create_wiki(
            session=db_session,
            user=admin_user,
            project_key=public_project.key,
            title="Admin Wiki Bypass",
            text="Content here.",
        )
        assert "Admin Wiki Bypass" in result

    async def test_admin_can_log_time_without_membership(
        self,
        db_session: AsyncSession,
        admin_user: User,
        public_project: Project,
        seed: dict,
    ):
        """Admin logs time with no Member row — check_permission bypassed."""
        from specivo.mcp.tools import _log_time

        result = await _log_time(
            session=db_session,
            user=admin_user,
            project_key=public_project.key,
            hours=Decimal("2.0"),
            activity_id=seed["activity"].id,
        )
        assert "2" in result
        assert public_project.key in result

    async def test_admin_can_create_version_without_membership(
        self,
        db_session: AsyncSession,
        admin_user: User,
        public_project: Project,
    ):
        """Admin creates a version with no Member row — check_permission bypassed."""
        from specivo.mcp.tools import _create_version

        result = await _create_version(
            session=db_session,
            user=admin_user,
            project_key=public_project.key,
            name="v2.0-admin-bypass",
        )
        assert "v2.0-admin-bypass" in result
