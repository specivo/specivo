"""Security integration tests for issue visibility, permission checks,
JWT blocklist fail-closed, and mass assignment protection.

Tests validate visibility, permissions, and authorization enforcement.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.issue import Issue
from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.role import Role
from specivo.models.user import User
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import UserFactory

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def lookup_data(db_session: AsyncSession):
    """Seed lookup tables (tracker, status, priority)."""
    status_obj = StatusFactory.build(name="New", position=1, category="backlog")
    tracker = TrackerFactory.build(name="Bug", default_status_id=None)
    priority = PriorityFactory.build(name="Normal", is_default=True)
    db_session.add_all([status_obj, tracker, priority])
    await db_session.commit()
    await db_session.refresh(status_obj)
    await db_session.refresh(tracker)
    await db_session.refresh(priority)
    # Set tracker default status after status has an ID
    tracker.default_status_id = status_obj.id
    await db_session.commit()
    return {"status": status_obj, "tracker": tracker, "priority": priority}


@pytest_asyncio.fixture
async def role_developer(db_session: AsyncSession) -> Role:
    """Developer role with default visibility and basic permissions."""
    result = await db_session.execute(select(Role).where(Role.name == "Developer"))
    existing = result.scalar_one_or_none()
    if existing:
        return existing
    role = Role(
        name="Developer",
        position=2,
        permissions=["add_issues", "edit_issues", "view_issues", "add_issue_notes"],
        issues_visibility="default",
        builtin=0,
    )
    db_session.add(role)
    await db_session.commit()
    await db_session.refresh(role)
    return role


@pytest_asyncio.fixture
async def role_manager(db_session: AsyncSession) -> Role:
    """Manager role with 'all' visibility and full permissions."""
    result = await db_session.execute(select(Role).where(Role.name == "Manager"))
    existing = result.scalar_one_or_none()
    if existing:
        return existing
    role = Role(
        name="Manager",
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
async def role_viewer(db_session: AsyncSession) -> Role:
    """Viewer role with 'own' visibility and view-only permissions."""
    role = Role(
        name="Viewer",
        position=3,
        permissions=["view_issues"],
        issues_visibility="own",
        builtin=0,
    )
    db_session.add(role)
    await db_session.commit()
    await db_session.refresh(role)
    return role


@pytest_asyncio.fixture
async def public_project(db_session: AsyncSession, lookup_data) -> Project:
    """Public project."""
    project = ProjectFactory.build(key="PUB", name="Public Project", is_public=True)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    return project


@pytest_asyncio.fixture
async def private_project(db_session: AsyncSession, lookup_data) -> Project:
    """Private project."""
    project = ProjectFactory.build(key="PRIV", name="Private Project", is_public=False)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    return project


async def _create_issue(
    db_session: AsyncSession,
    project: Project,
    lookup_data: dict,
    author: User,
    *,
    is_private: bool = False,
    assigned_to_id: int | None = None,
) -> Issue:
    """Helper to create an issue directly in the DB."""
    project.issue_sequence += 1
    issue = Issue(
        project_id=project.id,
        project_key=project.key,
        sequence_number=project.issue_sequence,
        tracker_id=lookup_data["tracker"].id,
        status_id=lookup_data["status"].id,
        priority_id=lookup_data["priority"].id,
        author_id=author.id,
        assigned_to_id=assigned_to_id,
        subject=f"Test Issue {project.issue_sequence}",
        is_private=is_private,
    )
    db_session.add(issue)
    await db_session.commit()
    await db_session.refresh(issue)
    return issue


async def _add_member(
    db_session: AsyncSession,
    project: Project,
    user: User,
    role: Role,
) -> Member:
    """Add a user as a project member with a role."""
    member = Member(user_id=user.id, project_id=project.id)
    db_session.add(member)
    await db_session.flush()
    mr = MemberRole(member_id=member.id, role_id=role.id)
    db_session.add(mr)
    await db_session.commit()
    await db_session.refresh(member)
    return member


# ===========================================================================
# Test: Private issue not visible to non-owner
# ===========================================================================


@pytest.mark.asyncio
class TestIssueVisibility:
    """Visibility checks: private issues, role-based visibility, admin bypass."""

    async def test_user_cannot_see_private_issue_not_owned(
        self,
        auth_client: AsyncClient,
        db_session: AsyncSession,
        lookup_data,
        private_project,
        role_developer,
    ):
        """Member with 'default' visibility cannot see private issue they don't own."""
        user = auth_client.state.user
        other_user = UserFactory.build(login="other_author", status="active")
        db_session.add(other_user)
        await db_session.commit()
        await db_session.refresh(other_user)

        await _add_member(db_session, private_project, user, role_developer)
        issue = await _create_issue(db_session, private_project, lookup_data, other_user, is_private=True)
        resp = await auth_client.get(f"/api/v1/issues/{issue.display_key}/")
        assert resp.status_code == 404

    async def test_user_can_see_own_private_issue(
        self,
        auth_client: AsyncClient,
        db_session: AsyncSession,
        lookup_data,
        private_project,
        role_developer,
    ):
        """Author can see their own private issue with 'default' visibility."""
        user = auth_client.state.user
        await _add_member(db_session, private_project, user, role_developer)
        issue = await _create_issue(db_session, private_project, lookup_data, user, is_private=True)
        resp = await auth_client.get(f"/api/v1/issues/{issue.display_key}/")
        assert resp.status_code == 200

    async def test_assignee_can_see_private_issue(
        self,
        auth_client: AsyncClient,
        db_session: AsyncSession,
        lookup_data,
        private_project,
        role_developer,
    ):
        """Assignee can see a private issue they don't own but are assigned to."""
        user = auth_client.state.user
        other_user = UserFactory.build(login="priv_author", status="active")
        db_session.add(other_user)
        await db_session.commit()
        await db_session.refresh(other_user)

        await _add_member(db_session, private_project, user, role_developer)
        issue = await _create_issue(
            db_session,
            private_project,
            lookup_data,
            other_user,
            is_private=True,
            assigned_to_id=user.id,
        )
        resp = await auth_client.get(f"/api/v1/issues/{issue.display_key}/")
        assert resp.status_code == 200

    async def test_non_member_cannot_see_issues_in_private_project(
        self,
        auth_client: AsyncClient,
        db_session: AsyncSession,
        lookup_data,
        private_project,
    ):
        """Non-member gets 404 on issues in a private project."""
        other_user = UserFactory.build(login="proj_author", status="active")
        db_session.add(other_user)
        await db_session.commit()
        await db_session.refresh(other_user)

        issue = await _create_issue(db_session, private_project, lookup_data, other_user)
        resp = await auth_client.get(f"/api/v1/issues/{issue.display_key}/")
        assert resp.status_code == 404

    async def test_admin_can_see_all_issues(
        self,
        admin_client: AsyncClient,
        db_session: AsyncSession,
        lookup_data,
        private_project,
    ):
        """Admin bypasses all visibility checks."""
        other_user = UserFactory.build(login="admin_test_author", status="active")
        db_session.add(other_user)
        await db_session.commit()
        await db_session.refresh(other_user)

        issue = await _create_issue(db_session, private_project, lookup_data, other_user, is_private=True)
        resp = await admin_client.get(f"/api/v1/issues/{issue.display_key}/")
        assert resp.status_code == 200

    async def test_non_member_can_see_public_project_non_private_issues(
        self,
        auth_client: AsyncClient,
        db_session: AsyncSession,
        lookup_data,
        public_project,
    ):
        """Non-member can see non-private issues in public project."""
        other_user = UserFactory.build(login="pub_author", status="active")
        db_session.add(other_user)
        await db_session.commit()
        await db_session.refresh(other_user)

        issue = await _create_issue(db_session, public_project, lookup_data, other_user)
        resp = await auth_client.get(f"/api/v1/issues/{issue.display_key}/")
        assert resp.status_code == 200

    async def test_non_member_cannot_see_private_issues_in_public_project(
        self,
        auth_client: AsyncClient,
        db_session: AsyncSession,
        lookup_data,
        public_project,
    ):
        """Non-member cannot see private issues even in public project."""
        other_user = UserFactory.build(login="pub_priv_author", status="active")
        db_session.add(other_user)
        await db_session.commit()
        await db_session.refresh(other_user)

        issue = await _create_issue(db_session, public_project, lookup_data, other_user, is_private=True)
        resp = await auth_client.get(f"/api/v1/issues/{issue.display_key}/")
        assert resp.status_code == 404

    async def test_list_issues_filters_by_visibility(
        self,
        auth_client: AsyncClient,
        db_session: AsyncSession,
        lookup_data,
        private_project,
        role_developer,
    ):
        """list_issues only returns issues visible to the user."""
        user = auth_client.state.user
        other_user = UserFactory.build(login="list_author", status="active")
        db_session.add(other_user)
        await db_session.commit()
        await db_session.refresh(other_user)

        await _add_member(db_session, private_project, user, role_developer)

        # Create one visible (own) and one invisible (private, other author)
        await _create_issue(db_session, private_project, lookup_data, user)
        await _create_issue(db_session, private_project, lookup_data, other_user, is_private=True)

        resp = await auth_client.get(f"/api/v1/projects/{private_project.key}/issues/?status=all")
        assert resp.status_code == 200
        data = resp.json()
        # Only the user's own issue should be visible (private issue hidden)
        assert data["total_count"] == 1

    async def test_own_visibility_only_sees_own_issues(
        self,
        auth_client: AsyncClient,
        db_session: AsyncSession,
        lookup_data,
        private_project,
        role_viewer,
    ):
        """User with 'own' visibility only sees issues they authored or are assigned to."""
        user = auth_client.state.user
        other_user = UserFactory.build(login="own_vis_author", status="active")
        db_session.add(other_user)
        await db_session.commit()
        await db_session.refresh(other_user)

        await _add_member(db_session, private_project, user, role_viewer)

        # Create own issue + other's issue (non-private)
        await _create_issue(db_session, private_project, lookup_data, user)
        await _create_issue(db_session, private_project, lookup_data, other_user)

        resp = await auth_client.get(f"/api/v1/projects/{private_project.key}/issues/?status=all")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_count"] == 1


# ===========================================================================
# Test: Permission checks on write operations
# ===========================================================================


@pytest.mark.asyncio
class TestIssuePermissions:
    """Permission enforcement on create, update, delete."""

    async def test_create_requires_add_issues_permission(
        self,
        auth_client: AsyncClient,
        db_session: AsyncSession,
        lookup_data,
        private_project,
        role_viewer,
    ):
        """User without add_issues permission gets 403 on create."""
        user = auth_client.state.user
        await _add_member(db_session, private_project, user, role_viewer)

        resp = await auth_client.post(
            f"/api/v1/projects/{private_project.key}/issues/",
            json={
                "project_key": private_project.key,
                "tracker_id": lookup_data["tracker"].id,
                "subject": "Should fail",
            },
        )
        assert resp.status_code == 403

    async def test_create_succeeds_with_add_issues_permission(
        self,
        auth_client: AsyncClient,
        db_session: AsyncSession,
        lookup_data,
        private_project,
        role_developer,
    ):
        """User with add_issues permission can create issues."""
        user = auth_client.state.user
        await _add_member(db_session, private_project, user, role_developer)

        resp = await auth_client.post(
            f"/api/v1/projects/{private_project.key}/issues/",
            json={
                "project_key": private_project.key,
                "tracker_id": lookup_data["tracker"].id,
                "subject": "Should succeed",
            },
        )
        assert resp.status_code == 201

    async def test_update_requires_edit_issues_permission(
        self,
        auth_client: AsyncClient,
        db_session: AsyncSession,
        lookup_data,
        private_project,
        role_viewer,
    ):
        """User without edit_issues permission gets 403 on update."""
        user = auth_client.state.user
        await _add_member(db_session, private_project, user, role_viewer)
        issue = await _create_issue(db_session, private_project, lookup_data, user)

        resp = await auth_client.patch(
            f"/api/v1/issues/{issue.display_key}/",
            json={"subject": "Updated", "lock_version": issue.lock_version},
        )
        assert resp.status_code == 403

    async def test_delete_requires_delete_issues_permission(
        self,
        auth_client: AsyncClient,
        db_session: AsyncSession,
        lookup_data,
        private_project,
        role_developer,
    ):
        """Developer role does not have delete_issues, so delete returns 403."""
        user = auth_client.state.user
        await _add_member(db_session, private_project, user, role_developer)
        issue = await _create_issue(db_session, private_project, lookup_data, user)

        resp = await auth_client.delete(f"/api/v1/issues/{issue.display_key}/")
        assert resp.status_code == 403

    async def test_admin_can_create_without_membership(
        self,
        admin_client: AsyncClient,
        db_session: AsyncSession,
        lookup_data,
        private_project,
    ):
        """Admin bypasses permission checks."""
        resp = await admin_client.post(
            f"/api/v1/projects/{private_project.key}/issues/",
            json={
                "project_key": private_project.key,
                "tracker_id": lookup_data["tracker"].id,
                "subject": "Admin issue",
            },
        )
        assert resp.status_code == 201


# ===========================================================================
# Test: Project sub-resource access checks
# ===========================================================================


@pytest.mark.asyncio
class TestProjectSubResourceAccess:
    """Members and modules endpoints respect project visibility."""

    async def test_non_member_cannot_list_members_of_private_project(
        self,
        auth_client: AsyncClient,
        db_session: AsyncSession,
        private_project,
    ):
        """Non-member gets 404 on members of private project."""
        resp = await auth_client.get(f"/api/v1/projects/{private_project.key}/members/")
        assert resp.status_code == 404

    async def test_non_member_cannot_get_modules_of_private_project(
        self,
        auth_client: AsyncClient,
        db_session: AsyncSession,
        private_project,
    ):
        """Non-member gets 404 on modules of private project."""
        resp = await auth_client.get(f"/api/v1/projects/{private_project.key}/modules/")
        assert resp.status_code == 404

    async def test_private_project_returns_404_not_403(
        self,
        auth_client: AsyncClient,
        db_session: AsyncSession,
        private_project,
    ):
        """GET /projects/{key} for non-member on private project returns 404."""
        resp = await auth_client.get(f"/api/v1/projects/{private_project.key}/")
        assert resp.status_code == 404
        # Should not reveal the project exists
        body = resp.json()
        assert "permission" not in body["errors"][0]["message"].lower()


# ===========================================================================
# Test: JWT blocklist fail-closed
# ===========================================================================


@pytest.mark.asyncio
class TestJwtBlocklistFailClosed:
    """JWT blocklist returns True (deny) when Redis is unavailable."""

    async def test_redis_failure_denies_jwt(self):
        """When Redis is unavailable, is_token_blocked returns True."""
        from unittest.mock import patch

        from specivo.core.security import is_token_blocked

        with patch(
            "specivo.core.redis.get_redis",
            side_effect=ConnectionError("Redis down"),
        ):
            result = await is_token_blocked("some-jti")
            assert result is True


# ===========================================================================
# Test: Mass assignment protection
# ===========================================================================


@pytest.mark.asyncio
class TestMassAssignment:
    """Verify that dangerous fields cannot be set via API."""

    async def test_cannot_set_author_id_via_create(
        self,
        auth_client: AsyncClient,
        db_session: AsyncSession,
        lookup_data,
        public_project,
        role_developer,
    ):
        """author_id is set from the authenticated user, not from request body."""
        user = auth_client.state.user
        await _add_member(db_session, public_project, user, role_developer)

        resp = await auth_client.post(
            f"/api/v1/projects/{public_project.key}/issues/",
            json={
                "project_key": public_project.key,
                "tracker_id": lookup_data["tracker"].id,
                "subject": "Mass assignment test",
                "author_id": 99999,  # Should be ignored
            },
        )
        # Pydantic ignores extra fields by default, so the request should
        # succeed but author_id should be the authenticated user's ID
        assert resp.status_code == 201
        data = resp.json()
        assert data["author"]["id"] == user.id

    async def test_cannot_set_id_via_create(
        self,
        auth_client: AsyncClient,
        db_session: AsyncSession,
        lookup_data,
        public_project,
        role_developer,
    ):
        """Internal ID cannot be set via request body."""
        user = auth_client.state.user
        await _add_member(db_session, public_project, user, role_developer)

        resp = await auth_client.post(
            f"/api/v1/projects/{public_project.key}/issues/",
            json={
                "project_key": public_project.key,
                "tracker_id": lookup_data["tracker"].id,
                "subject": "ID injection test",
                "id": 99999,
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["id"] != 99999


# ===========================================================================
# Test: SECRET_KEY minimum length
# ===========================================================================


class TestSecretKeyValidation:
    """Validate that SECRET_KEY must be at least 32 bytes."""

    def test_short_secret_key_raises(self):
        from pydantic import ValidationError as PydanticValidationError

        from specivo.core.config import Settings

        with pytest.raises(PydanticValidationError, match="SECRET_KEY must be at least 32 bytes"):
            Settings(
                database_url="postgresql+asyncpg://x:x@localhost/x",
                redis_url="redis://localhost",
                secret_key="tooshort",
            )

    def test_valid_secret_key_passes(self):
        from specivo.core.config import Settings

        s = Settings(
            database_url="postgresql+asyncpg://x:x@localhost/x",
            redis_url="redis://localhost",
            secret_key="a" * 32,
        )
        assert len(s.secret_key) >= 32
