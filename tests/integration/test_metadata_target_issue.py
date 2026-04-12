"""Integration tests for IssueMetadataTarget — the core metadata target."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.metadata_targets import (
    IssueMetadataTarget,
    get_metadata_target_registry,
)
from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.project import Project
from specivo.models.user import User
from specivo.schemas.issue import IssueCreate
from specivo.services.issue_service import IssueService
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory

pytestmark = pytest.mark.asyncio(loop_scope="function")


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
    proj = ProjectFactory.build(key="MTGT", name="Metadata Target Test", is_public=True)
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


@pytest_asyncio.fixture
async def admin(db_session: AsyncSession) -> User:
    user = AdminUserFactory.build(login="mtgt_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def issue(db_session, project, tracker, priority, status, admin):
    svc = IssueService()
    issue = await svc.create(
        db_session,
        project,
        IssueCreate(project_key=project.key, tracker_id=tracker.id, subject="Target test"),
        admin,
    )
    await db_session.commit()
    return issue


class TestIssueMetadataTarget:
    async def test_registry_returns_issue_target(self):
        reg = get_metadata_target_registry()
        target = reg.get("issue")
        assert isinstance(target, IssueMetadataTarget)

    async def test_resolve_by_display_key(self, db_session, admin, issue):
        target = IssueMetadataTarget()
        resolved = await target.resolve(db_session, issue.display_key, admin)
        assert resolved.id == issue.id

    async def test_get_metadata_initially_empty(self, db_session, admin, issue):
        target = IssueMetadataTarget()
        meta = target.get_metadata(issue)
        assert meta == {}

    async def test_set_metadata_persists(self, db_session, admin, issue):
        target = IssueMetadataTarget()
        updated = await target.set_metadata(
            db_session, issue, {"severity": "high"}, admin
        )
        assert updated.issue_metadata == {"severity": "high"}
        assert target.project_id_of(updated) == issue.project_id
        assert target.display_ref(updated) == issue.display_key

    async def test_set_metadata_bumps_lock_version(self, db_session, admin, issue):
        target = IssueMetadataTarget()
        before = issue.lock_version
        updated = await target.set_metadata(db_session, issue, {"a": 1}, admin)
        assert updated.lock_version > before
