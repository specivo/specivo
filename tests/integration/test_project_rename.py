"""Integration tests for project key/identifier rename.

Tests cover:
- Key rename re-keys all issues
- Old key alias created for redirect
- Old key lookup returns current project
- Key conflict returns 409
- Identifier rename updates path
- Admin-only (non-admin gets 403, project manager gets 403)
- Validation (empty body, same key)
- Alias revert (rename back to a previous key)
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.issue import Issue
from specivo.models.member import Member, MemberRole
from specivo.models.project import Project, ProjectKeyAlias
from specivo.models.role import Role
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import UserFactory

pytestmark = pytest.mark.integration

RENAME_URL = "/api/v1/admin/projects/{key}/rename/"


async def _create_user(db: AsyncSession, **kwargs):
    user = UserFactory.build(**kwargs)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _create_project(db: AsyncSession, **kwargs) -> Project:
    proj = ProjectFactory.build(**kwargs)
    db.add(proj)
    await db.commit()
    await db.refresh(proj)
    return proj


async def _seed_lookups(db: AsyncSession):
    status = StatusFactory.build(name="Open", position=1, is_closed=False)
    db.add(status)
    await db.flush()
    tracker = TrackerFactory.build(name="Task", default_status_id=status.id)
    db.add(tracker)
    priority = PriorityFactory.build(name="Normal", is_default=True, position=1)
    db.add(priority)
    await db.commit()
    await db.refresh(status)
    await db.refresh(tracker)
    await db.refresh(priority)
    return tracker, status, priority


async def _create_issue(db: AsyncSession, project: Project, tracker, status, priority, user, seq: int) -> Issue:
    issue = Issue(
        project_id=project.id,
        project_key=project.key,
        sequence_number=seq,
        tracker_id=tracker.id,
        status_id=status.id,
        priority_id=priority.id,
        author_id=user.id,
        subject=f"Issue {seq}",
    )
    db.add(issue)
    await db.flush()
    return issue


# ---------------------------------------------------------------------------
# Key rename
# ---------------------------------------------------------------------------


class TestRenameKey:
    @pytest_asyncio.fixture
    async def setup(self, admin_client: AsyncClient, db_session: AsyncSession):
        tracker, status, priority = await _seed_lookups(db_session)
        project = await _create_project(db_session, key="OLD", identifier="old-proj", path="old_proj")
        admin = admin_client.state.user
        for i in range(1, 4):
            await _create_issue(db_session, project, tracker, status, priority, admin, i)
        await db_session.commit()
        return project

    async def test_rename_updates_issues(self, admin_client: AsyncClient, db_session: AsyncSession, setup):
        project = setup
        resp = await admin_client.post(
            RENAME_URL.format(key="OLD"),
            json={"new_key": "NEW"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["key"] == "NEW"
        assert data["old_key"] == "OLD"
        assert data["issues_rekeyed"] == 3

        # Verify issues in DB
        result = await db_session.execute(select(Issue).where(Issue.project_id == project.id))
        issues = result.scalars().all()
        for issue in issues:
            assert issue.project_key == "NEW"

    async def test_rename_creates_alias(self, admin_client: AsyncClient, db_session: AsyncSession, setup):
        resp = await admin_client.post(
            RENAME_URL.format(key="OLD"),
            json={"new_key": "NEW"},
        )
        assert resp.status_code == 200

        alias = await db_session.execute(
            select(ProjectKeyAlias).where(ProjectKeyAlias.old_key == "OLD")
        )
        assert alias.scalar_one_or_none() is not None

    async def test_old_key_lookup_resolves(self, admin_client: AsyncClient, db_session: AsyncSession, setup):
        await admin_client.post(RENAME_URL.format(key="OLD"), json={"new_key": "NEW"})

        # GET project using old key should resolve
        resp = await admin_client.get("/api/v1/projects/OLD/")
        assert resp.status_code == 200
        assert resp.json()["key"] == "NEW"

    async def test_key_conflict_returns_409(self, admin_client: AsyncClient, db_session: AsyncSession, setup):
        await _create_project(db_session, key="TAKEN", identifier="taken-proj", path="taken_proj")
        resp = await admin_client.post(
            RENAME_URL.format(key="OLD"),
            json={"new_key": "TAKEN"},
        )
        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Identifier rename
# ---------------------------------------------------------------------------


class TestRenameIdentifier:
    async def test_identifier_rename_updates_path(
        self, admin_client: AsyncClient, db_session: AsyncSession
    ):
        await _create_project(db_session, key="IDTEST", identifier="id-test", path="id_test")
        resp = await admin_client.post(
            RENAME_URL.format(key="IDTEST"),
            json={"new_identifier": "new-id"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["identifier"] == "new-id"
        assert data["old_identifier"] == "id-test"
        assert "new_id" in data["path"]

    async def test_identifier_conflict_returns_409(
        self, admin_client: AsyncClient, db_session: AsyncSession
    ):
        await _create_project(db_session, key="IDA", identifier="id-a", path="id_a")
        await _create_project(db_session, key="IDB", identifier="id-b", path="id_b")
        resp = await admin_client.post(
            RENAME_URL.format(key="IDA"),
            json={"new_identifier": "id-b"},
        )
        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Permission checks
# ---------------------------------------------------------------------------


class TestRenamePermissions:
    async def test_non_admin_gets_403(self, auth_client: AsyncClient, db_session: AsyncSession):
        await _create_project(db_session, key="PERM", identifier="perm-proj", path="perm_proj")
        resp = await auth_client.post(
            RENAME_URL.format(key="PERM"),
            json={"new_key": "NEWP"},
        )
        assert resp.status_code == 403

    async def test_project_manager_gets_403(self, auth_client: AsyncClient, db_session: AsyncSession):
        """Project manager with manage_project permission still cannot rename."""
        project = await _create_project(db_session, key="MGR", identifier="mgr-proj", path="mgr_proj")
        user = auth_client.state.user

        # Give user manage_project permission
        role = Role(name="Manager-rename-test", permissions=["*"], builtin=0, issues_visibility="all")
        db_session.add(role)
        await db_session.flush()
        member = Member(user_id=user.id, project_id=project.id)
        db_session.add(member)
        await db_session.flush()
        db_session.add(MemberRole(member_id=member.id, role_id=role.id))
        await db_session.commit()

        resp = await auth_client.post(
            RENAME_URL.format(key="MGR"),
            json={"new_key": "NEWM"},
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestRenameValidation:
    async def test_empty_body_returns_422(self, admin_client: AsyncClient, db_session: AsyncSession):
        await _create_project(db_session, key="VAL", identifier="val-proj", path="val_proj")
        resp = await admin_client.post(
            RENAME_URL.format(key="VAL"),
            json={},
        )
        assert resp.status_code == 422

    async def test_project_not_found_returns_404(self, admin_client: AsyncClient):
        resp = await admin_client.post(
            RENAME_URL.format(key="NONEXISTENT"),
            json={"new_key": "WHAT"},
        )
        assert resp.status_code == 404

    async def test_invalid_key_format_returns_422(self, admin_client: AsyncClient, db_session: AsyncSession):
        await _create_project(db_session, key="FMT", identifier="fmt-proj", path="fmt_proj")
        resp = await admin_client.post(
            RENAME_URL.format(key="FMT"),
            json={"new_key": "A"},  # too short — must be 2-10 chars
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Alias revert
# ---------------------------------------------------------------------------


class TestAliasRevert:
    async def test_revert_to_previous_key(self, admin_client: AsyncClient, db_session: AsyncSession):
        """Renaming back to a previous key should delete the alias and succeed."""
        await _create_project(db_session, key="REV", identifier="rev-proj", path="rev_proj")

        # Rename REV → TEMP
        resp1 = await admin_client.post(RENAME_URL.format(key="REV"), json={"new_key": "TEMP"})
        assert resp1.status_code == 200

        # Rename TEMP → REV (revert)
        resp2 = await admin_client.post(RENAME_URL.format(key="TEMP"), json={"new_key": "REV"})
        assert resp2.status_code == 200
        assert resp2.json()["key"] == "REV"

        # Old alias for REV should be gone (replaced by TEMP alias)
        alias = await db_session.execute(
            select(ProjectKeyAlias).where(ProjectKeyAlias.old_key == "REV")
        )
        assert alias.scalar_one_or_none() is None
