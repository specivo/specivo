"""Integration tests for MCP agent audit logging.

Tests call tool functions directly (same pattern as test_mcp_server.py)
and verify that audit log entries are created with source=mcp.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.models.auth import ApiKey
from specivo.models.lookups import IssuePriority, IssueStatus, Tracker
from specivo.models.project import Project
from specivo.models.security_audit import SecurityAuditLog
from specivo.models.user import User
from specivo.services.api_key_service import ApiKeyService
from tests.factories.lookups import PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _set_mcp_key(raw_key: str) -> None:
    from specivo.mcp.auth import mcp_raw_key_var

    mcp_raw_key_var.set(raw_key)


def _clear_mcp_key() -> None:
    from specivo.mcp.auth import mcp_raw_key_var

    mcp_raw_key_var.set(None)


async def _get_mcp_audit_events(db: AsyncSession) -> list[SecurityAuditLog]:
    """Get all audit events with source=mcp in details."""
    result = await db.execute(
        select(SecurityAuditLog).order_by(SecurityAuditLog.created_at.desc())
    )
    all_events = result.scalars().all()
    return [e for e in all_events if e.details.get("source") == "mcp"]


async def _get_audit_events_by_type(db: AsyncSession, event_type: str) -> list[SecurityAuditLog]:
    result = await db.execute(
        select(SecurityAuditLog)
        .where(SecurityAuditLog.event_type == event_type)
        .order_by(SecurityAuditLog.created_at.desc())
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def agent_user(db_session: AsyncSession) -> User:
    user = AdminUserFactory.build(login="mcp_agent", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def agent_api_key(db_session: AsyncSession, agent_user: User) -> tuple[ApiKey, str]:
    svc = ApiKeyService()
    key, raw = await svc.create_key(db_session, agent_user.id, name="mcp-test")
    await db_session.commit()
    await db_session.refresh(key)
    return key, raw


@pytest_asyncio.fixture
async def status(db_session: AsyncSession) -> IssueStatus:
    s = StatusFactory.build(name="New", position=1, is_closed=False)
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
    proj = ProjectFactory.build(key="AUDIT", identifier="audit-proj")
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


# ---------------------------------------------------------------------------
# Tool call audit logging
# ---------------------------------------------------------------------------


class TestMcpToolAuditLogging:
    async def test_list_projects_writes_audit_log(
        self, db_session: AsyncSession, agent_user: User, agent_api_key, project: Project
    ):
        from specivo.mcp.tools import _list_projects

        _set_mcp_key(agent_api_key[1])
        try:
            await _list_projects(db_session, agent_user)
            events = await _get_mcp_audit_events(db_session)
            assert len(events) >= 1, f"Expected MCP audit event, got {len(events)}"
            assert events[0].details["tool"] == "list_projects"
            assert events[0].details["source"] == "mcp"
        finally:
            _clear_mcp_key()

    async def test_create_issue_writes_audit_log(
        self, db_session: AsyncSession, agent_user: User, agent_api_key,
        project: Project, tracker: Tracker, status: IssueStatus, priority: IssuePriority,
    ):
        from specivo.mcp.tools import _create_issue

        _set_mcp_key(agent_api_key[1])
        try:
            await _create_issue(
                db_session, agent_user, "AUDIT", tracker.id,
                "Test issue for audit", "desc",
            )
            events = await _get_mcp_audit_events(db_session)
            assert len(events) >= 1
            assert events[0].details["tool"] == "create_issue"
            assert events[0].details["project_key"] == "AUDIT"
            assert events[0].event_type == "issue_created"
        finally:
            _clear_mcp_key()

    async def test_update_issue_writes_audit_log(
        self, db_session: AsyncSession, agent_user: User, agent_api_key,
        project: Project, tracker: Tracker, status: IssueStatus, priority: IssuePriority,
    ):
        from specivo.mcp.tools import _create_issue, _update_issue

        _set_mcp_key(agent_api_key[1])
        try:
            await _create_issue(
                db_session, agent_user, "AUDIT", tracker.id,
                "Update test", "",
            )
            await _update_issue(db_session, agent_user, "AUDIT-1", subject="Updated subject")
            events = await _get_mcp_audit_events(db_session)
            update_events = [e for e in events if e.details.get("tool") == "update_issue"]
            assert len(update_events) >= 1
            assert update_events[0].event_type == "issue_updated"
        finally:
            _clear_mcp_key()

    async def test_search_writes_audit_log(
        self, db_session: AsyncSession, agent_user: User, agent_api_key, project: Project,
    ):
        from specivo.mcp.tools import _search

        _set_mcp_key(agent_api_key[1])
        try:
            await _search(db_session, agent_user, "banana")
            events = await _get_mcp_audit_events(db_session)
            search_events = [e for e in events if e.details.get("tool") == "search"]
            assert len(search_events) >= 1
            assert search_events[0].details["query"] == "banana"
            assert search_events[0].event_type == "search_query"
        finally:
            _clear_mcp_key()

    async def test_add_comment_writes_audit_log(
        self, db_session: AsyncSession, agent_user: User, agent_api_key,
        project: Project, tracker: Tracker, status: IssueStatus, priority: IssuePriority,
    ):
        from specivo.mcp.tools import _add_comment, _create_issue

        _set_mcp_key(agent_api_key[1])
        try:
            await _create_issue(
                db_session, agent_user, "AUDIT", tracker.id,
                "Comment test", "",
            )
            await _add_comment(db_session, agent_user, "AUDIT-1", "test note")
            events = await _get_mcp_audit_events(db_session)
            comment_events = [e for e in events if e.details.get("tool") == "add_comment"]
            assert len(comment_events) >= 1
            assert comment_events[0].event_type == "comment_added"
        finally:
            _clear_mcp_key()

    async def test_read_wiki_writes_audit_log(
        self, db_session: AsyncSession, agent_user: User, agent_api_key, project: Project,
    ):
        from specivo.mcp.tools import _read_wiki
        from specivo.services.wiki_service import WikiService

        # Create a wiki page first
        wiki_svc = WikiService()
        await wiki_svc.get_or_create_wiki(db_session, project.id)
        await db_session.flush()
        page, _content = await wiki_svc.create_page(
            db_session, project.id, "TestPage", "Test content", agent_user
        )
        await db_session.commit()

        _set_mcp_key(agent_api_key[1])
        try:
            await _read_wiki(db_session, agent_user, "AUDIT", page.slug)
            events = await _get_mcp_audit_events(db_session)
            read_events = [e for e in events if e.details.get("tool") == "read_wiki"]
            assert len(read_events) >= 1
            assert read_events[0].event_type == "wiki_read"
        finally:
            _clear_mcp_key()

    async def test_audit_log_includes_source_mcp(
        self, db_session: AsyncSession, agent_user: User, agent_api_key, project: Project,
    ):
        """All MCP tool audit entries must have source=mcp in details."""
        from specivo.mcp.tools import _list_projects

        _set_mcp_key(agent_api_key[1])
        try:
            await _list_projects(db_session, agent_user)
            events = await _get_mcp_audit_events(db_session)
            assert len(events) >= 1
            for e in events:
                assert e.details.get("source") == "mcp"
        finally:
            _clear_mcp_key()
