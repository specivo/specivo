"""Integration tests for the recurring-pattern MCP tools.

Covers create / list / update / delete round-trip, skip occurrence (and that a
skipped occurrence is omitted from the preview), occurrence preview, plus
permission enforcement and security-audit event emission. The implementation
functions are called directly with ``(db_session, user, ...)`` mirroring the
other MCP tool test suites.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.exceptions import PermissionDeniedError
from specivo.models.lookups import IssueStatus, Tracker
from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.recurring_pattern import RecurringPattern
from specivo.models.role import Role
from specivo.models.security_audit import SecurityAuditLog
from specivo.models.user import User
from tests.factories.lookups import StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.user import AdminUserFactory, UserFactory

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.serial]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def status_open(db_session: AsyncSession) -> IssueStatus:
    s = StatusFactory.build(name="New", position=1, category="backlog")
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


@pytest_asyncio.fixture
async def tracker(db_session: AsyncSession, status_open: IssueStatus) -> Tracker:
    t = TrackerFactory.build(name="Task", default_status_id=status_open.id)
    db_session.add(t)
    await db_session.commit()
    await db_session.refresh(t)
    return t


@pytest_asyncio.fixture
async def project(db_session: AsyncSession) -> Project:
    proj = ProjectFactory.build(key="RMCP", name="Recurring MCP", is_public=True)
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


@pytest_asyncio.fixture
async def admin(db_session: AsyncSession) -> User:
    user = AdminUserFactory.build(login="rmcp_admin", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def viewer(db_session: AsyncSession) -> User:
    user = UserFactory.build(login="rmcp_viewer", status="active")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def role_view_only(db_session: AsyncSession) -> Role:
    role = Role(
        name=f"RMCPViewOnly-{uuid.uuid4().hex[:8]}",
        position=21,
        assignable=True,
        builtin=0,
        permissions=["view_issues"],
        issues_visibility="default",
        settings={},
    )
    db_session.add(role)
    await db_session.commit()
    await db_session.refresh(role)
    return role


async def _add_member(db: AsyncSession, project: Project, user: User, role: Role) -> None:
    member = Member(user_id=user.id, project_id=project.id)
    db.add(member)
    await db.flush()
    db.add(MemberRole(member_id=member.id, role_id=role.id))
    await db.commit()


async def _audit_count(db: AsyncSession, event_type: str, project_id: int) -> int:
    result = await db.execute(
        select(SecurityAuditLog).where(
            SecurityAuditLog.event_type == event_type,
            SecurityAuditLog.project_id == project_id,
        )
    )
    return len(list(result.scalars().all()))


# A Monday, so a weekly-on-Monday rule is coherent.
_DTSTART = "2026-01-05T09:00:00+00:00"


async def _create(db: AsyncSession, user: User, project: Project, tracker: Tracker, **overrides):
    from specivo.mcp.tools import _create_recurring_pattern

    kwargs = {
        "name": "Weekly standup notes",
        "template_subject": "Standup notes",
        "template_tracker_id": tracker.id,
        "freq": "weekly",
        "dtstart": _DTSTART,
        "byday": "MO",
        "creation_lead_time_days": 60,
    }
    kwargs.update(overrides)
    return await _create_recurring_pattern(db, user, project.key, **kwargs)


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


class TestRecurringPatternRoundTrip:
    async def test_create_list_update_delete(self, db_session, admin, project, tracker):
        from specivo.mcp.tools import (
            _delete_recurring_pattern,
            _list_recurring_patterns,
            _update_recurring_pattern,
        )

        # Create
        out = await _create(db_session, admin, project, tracker)
        assert "Created recurring pattern" in out
        assert "Weekly standup notes" in out

        result = await db_session.execute(
            select(RecurringPattern).where(RecurringPattern.project_id == project.id)
        )
        rows = list(result.scalars().all())
        assert len(rows) == 1
        pattern = rows[0]
        assert pattern.byday == ["MO"]
        assert pattern.freq == "weekly"
        assert pattern.anchor_mode == "fixed"
        pid = pattern.id

        # List shows the new pattern with its id
        listing = await _list_recurring_patterns(db_session, admin, project.key)
        assert f"[{pid}]" in listing
        assert "Weekly standup notes" in listing
        assert "weekly/1" in listing

        # Update name + interval
        out = await _update_recurring_pattern(
            db_session, admin, project.key, pid, name="Weekly standup v2", rrule_interval=2
        )
        assert "Updated recurring pattern" in out
        await db_session.refresh(pattern)
        assert pattern.name == "Weekly standup v2"
        assert pattern.rrule_interval == 2

        # Delete
        out = await _delete_recurring_pattern(db_session, admin, project.key, pid)
        assert "Deleted recurring pattern" in out

        listing = await _list_recurring_patterns(db_session, admin, project.key)
        assert "Weekly standup v2" not in listing

    async def test_create_with_rrule_raw(self, db_session, admin, project, tracker):
        out = await _create(
            db_session,
            admin,
            project,
            tracker,
            byday=None,
            rrule_raw="FREQ=WEEKLY;BYDAY=MO",
        )
        assert "Created recurring pattern" in out
        result = await db_session.execute(
            select(RecurringPattern).where(RecurringPattern.project_id == project.id)
        )
        pattern = result.scalar_one()
        assert pattern.rrule_raw == "FREQ=WEEKLY;BYDAY=MO"

    async def test_create_incoherent_rule_returns_error(self, db_session, admin, project, tracker):
        # Weekly on a non-weekday token combined with a count that can never
        # be satisfied: rely on the engine surfacing an invalid rule. A clearly
        # bad timezone is the simplest reliable invalid-input path.
        out = await _create(db_session, admin, project, tracker, timezone="Mars/Phobos")
        assert out.startswith("Error:")


# ---------------------------------------------------------------------------
# Occurrences + skip
# ---------------------------------------------------------------------------


class TestRecurrenceOccurrences:
    async def test_list_occurrences_returns_upcoming(self, db_session, admin, project, tracker):
        from specivo.mcp.tools import _list_recurrence_occurrences

        # dtstart far in the past so occurrences land within the window from now.
        await _create(db_session, admin, project, tracker, dtstart="2020-01-06T09:00:00+00:00")
        result = await db_session.execute(
            select(RecurringPattern).where(RecurringPattern.project_id == project.id)
        )
        pid = result.scalar_one().id

        out = await _list_recurrence_occurrences(db_session, admin, project.key, pid, days=30)
        assert "Upcoming occurrences" in out
        # A weekly rule over 30 days yields at least 4 Mondays.
        assert out.count("T09:00:00") >= 4

    async def test_skip_omits_occurrence(self, db_session, admin, project, tracker):
        from specivo.mcp.tools import (
            _list_recurrence_occurrences,
            _skip_recurrence_occurrence,
        )

        await _create(db_session, admin, project, tracker, dtstart="2020-01-06T09:00:00+00:00")
        result = await db_session.execute(
            select(RecurringPattern).where(RecurringPattern.project_id == project.id)
        )
        pid = result.scalar_one().id

        before = await _list_recurrence_occurrences(db_session, admin, project.key, pid, days=30)
        # Pull the first occurrence datetime out of the preview text.
        first_line = next(
            ln.strip() for ln in before.splitlines() if "T09:00:00" in ln
        )
        first_occ = first_line.strip()

        out = await _skip_recurrence_occurrence(db_session, admin, project.key, pid, first_occ)
        assert "Skipped occurrence" in out

        after = await _list_recurrence_occurrences(db_session, admin, project.key, pid, days=30)
        assert first_occ not in after


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------


class TestRecurringPatternPermissions:
    async def test_create_denied_for_non_manager(
        self, db_session, viewer, project, tracker, role_view_only
    ):
        await _add_member(db_session, project, viewer, role_view_only)
        with pytest.raises(PermissionDeniedError):
            await _create(db_session, viewer, project, tracker)

    async def test_update_denied_for_non_manager(
        self, db_session, admin, viewer, project, tracker, role_view_only
    ):
        from specivo.mcp.tools import _update_recurring_pattern

        await _create(db_session, admin, project, tracker)
        result = await db_session.execute(
            select(RecurringPattern).where(RecurringPattern.project_id == project.id)
        )
        pid = result.scalar_one().id

        await _add_member(db_session, project, viewer, role_view_only)
        with pytest.raises(PermissionDeniedError):
            await _update_recurring_pattern(db_session, viewer, project.key, pid, name="hacked")

    async def test_delete_denied_for_non_manager(
        self, db_session, admin, viewer, project, tracker, role_view_only
    ):
        from specivo.mcp.tools import _delete_recurring_pattern

        await _create(db_session, admin, project, tracker)
        result = await db_session.execute(
            select(RecurringPattern).where(RecurringPattern.project_id == project.id)
        )
        pid = result.scalar_one().id

        await _add_member(db_session, project, viewer, role_view_only)
        with pytest.raises(PermissionDeniedError):
            await _delete_recurring_pattern(db_session, viewer, project.key, pid)

    async def test_skip_denied_for_non_manager(
        self, db_session, admin, viewer, project, tracker, role_view_only
    ):
        from specivo.mcp.tools import _skip_recurrence_occurrence

        await _create(db_session, admin, project, tracker)
        result = await db_session.execute(
            select(RecurringPattern).where(RecurringPattern.project_id == project.id)
        )
        pid = result.scalar_one().id

        await _add_member(db_session, project, viewer, role_view_only)
        with pytest.raises(PermissionDeniedError):
            await _skip_recurrence_occurrence(db_session, viewer, project.key, pid, _DTSTART)


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


class TestRecurringPatternAudit:
    async def test_create_emits_audit_event(self, db_session, admin, project, tracker):
        before = await _audit_count(db_session, "recurring_pattern_created", project.id)
        await _create(db_session, admin, project, tracker)
        after = await _audit_count(db_session, "recurring_pattern_created", project.id)
        assert after == before + 1

        result = await db_session.execute(
            select(SecurityAuditLog)
            .where(SecurityAuditLog.event_type == "recurring_pattern_created")
            .order_by(SecurityAuditLog.id.desc())
        )
        row = result.scalars().first()
        assert row is not None
        assert row.details.get("source") == "mcp"
        assert row.details.get("name") == "Weekly standup notes"
        assert row.project_id == project.id

    async def test_list_emits_audit_event(self, db_session, admin, project):
        from specivo.mcp.tools import _list_recurring_patterns

        before = await _audit_count(db_session, "recurring_patterns_listed", project.id)
        await _list_recurring_patterns(db_session, admin, project.key)
        after = await _audit_count(db_session, "recurring_patterns_listed", project.id)
        assert after == before + 1

    async def test_update_delete_skip_emit_audit_events(self, db_session, admin, project, tracker):
        from specivo.mcp.tools import (
            _delete_recurring_pattern,
            _skip_recurrence_occurrence,
            _update_recurring_pattern,
        )

        await _create(db_session, admin, project, tracker, dtstart="2020-01-06T09:00:00+00:00")
        result = await db_session.execute(
            select(RecurringPattern).where(RecurringPattern.project_id == project.id)
        )
        pid = result.scalar_one().id

        before_u = await _audit_count(db_session, "recurring_pattern_updated", project.id)
        await _update_recurring_pattern(db_session, admin, project.key, pid, name="renamed")
        assert await _audit_count(db_session, "recurring_pattern_updated", project.id) == before_u + 1

        before_s = await _audit_count(db_session, "recurrence_occurrence_skipped", project.id)
        await _skip_recurrence_occurrence(
            db_session, admin, project.key, pid, "2020-01-06T09:00:00+00:00"
        )
        assert await _audit_count(db_session, "recurrence_occurrence_skipped", project.id) == before_s + 1

        before_d = await _audit_count(db_session, "recurring_pattern_deleted", project.id)
        await _delete_recurring_pattern(db_session, admin, project.key, pid)
        assert await _audit_count(db_session, "recurring_pattern_deleted", project.id) == before_d + 1

    async def test_occurrences_emit_audit_event(self, db_session, admin, project, tracker):
        from specivo.mcp.tools import _list_recurrence_occurrences

        await _create(db_session, admin, project, tracker, dtstart="2020-01-06T09:00:00+00:00")
        result = await db_session.execute(
            select(RecurringPattern).where(RecurringPattern.project_id == project.id)
        )
        pid = result.scalar_one().id

        before = await _audit_count(db_session, "recurrence_occurrences_listed", project.id)
        await _list_recurrence_occurrences(db_session, admin, project.key, pid)
        after = await _audit_count(db_session, "recurrence_occurrences_listed", project.id)
        assert after == before + 1
