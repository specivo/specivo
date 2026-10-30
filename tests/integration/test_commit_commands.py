"""Commit commands end to end: ``Fixes KEY-1`` moves the issue and ``KEY-1 @2h`` logs time, as the matched user.

Celery is not running here: the endpoint's queueing is replaced by a recorder
and the tests run ``process_delivery`` on the test session themselves.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.utils import utcnow
from specivo.models.issue import Issue
from specivo.models.lookups import IssueStatus, Tracker
from specivo.models.member import Member, MemberRole
from specivo.models.project import Project
from specivo.models.role import Role
from specivo.models.time_entry import TimeEntry
from specivo.models.user import User
from specivo.models.workflow import WorkflowTransition
from specivo.services.repository_hooks import process_delivery
from specivo.services.workflow_service import WorkflowService
from tests.factories.issue import IssueFactory
from tests.factories.lookups import DoneStatusFactory, PriorityFactory, StatusFactory, TrackerFactory
from tests.factories.project import ProjectFactory
from tests.factories.time_entry import TimeEntryActivityFactory
from tests.factories.user import UserFactory
from tests.fixtures.forge_hooks import commit, hook_path, make_integration, push_payload, signed_headers

pytestmark = [pytest.mark.asyncio(loop_scope="function"), pytest.mark.integration]

DEV_EMAIL = "dev-commands@example.com"
COMMANDS = {"commands_enabled": True, "time_logging_enabled": True}


@dataclass
class World:
    project: Project
    tracker: Tracker
    new: IssueStatus
    fixed: IssueStatus
    issue: Issue
    dev: User
    role: Role


@pytest_asyncio.fixture
async def world(db_session: AsyncSession) -> World:
    tracker, priority = TrackerFactory.build(), PriorityFactory.build()
    new, fixed = StatusFactory.build(name="Cmd New"), DoneStatusFactory.build(name="Cmd Fixed")
    activity = TimeEntryActivityFactory.build(name="Cmd Development", is_default=True, active=True)
    dev = UserFactory.build(
        login="cmd_dev", email=DEV_EMAIL, email_verified_at=utcnow(), status="active", timezone="Asia/Bangkok"
    )
    project = ProjectFactory.build(key="CMD", identifier="cmd", name="Commands", is_public=False)
    db_session.add_all([tracker, priority, new, fixed, activity, dev, project])
    await db_session.flush()
    issue = IssueFactory.build(
        project_id=project.id,
        project_key=project.key,
        sequence_number=1,
        tracker_id=tracker.id,
        status_id=new.id,
        priority_id=priority.id,
        author_id=dev.id,
        subject="Login fails",
    )
    role = Role(name="Cmd Developer", permissions=["view_issues", "edit_issues", "log_time", "view_time_entries"])
    db_session.add_all([issue, role])
    await db_session.flush()
    member = Member(user_id=dev.id, project_id=project.id)
    db_session.add(member)
    await db_session.flush()
    db_session.add(MemberRole(member_id=member.id, role_id=role.id))
    await db_session.commit()
    return World(project, tracker, new, fixed, issue, dev, role)


@pytest.fixture
def queued(monkeypatch) -> list[int]:
    calls: list[int] = []

    def _record(ids):
        calls.extend(ids)
        return len(calls)

    monkeypatch.setattr("specivo.hooks.router.queue_processing", _record)
    return calls


async def _integration(db: AsyncSession, world: World, **settings):
    return await make_integration(
        db, world.project, settings={**COMMANDS, "fixing_status_id": world.fixed.id, **settings}
    )


async def _push(client, db, integration, commits, queued, *, branch="main", delivery=None):
    body = json.dumps(push_payload(integration.provider, commits, branch=branch)).encode()
    headers = signed_headers(integration.provider, body, delivery=delivery or str(uuid.uuid4()))
    resp = await client.post(hook_path(integration), content=body, headers=headers)
    assert resp.status_code == 202, resp.text
    result = await process_delivery(db, queued[-1])
    await db.commit()
    return result


async def _status(db: AsyncSession, issue: Issue) -> int:
    await db.refresh(issue)
    return issue.status_id


async def _entries(db: AsyncSession, issue: Issue) -> list[TimeEntry]:
    return list((await db.execute(select(TimeEntry).where(TimeEntry.issue_id == issue.id))).scalars().all())


class TestFixingKeyword:
    async def test_fixes_on_default_branch_moves_issue_as_matched_user(self, client, db_session, world, queued):
        integration = await _integration(db_session, world)
        result = await _push(client, db_session, integration, [commit("f1", "Fixes CMD-1", email=DEV_EMAIL)], queued)
        assert await _status(db_session, world.issue) == world.fixed.id
        assert "CMD-1 fixes: moved" in result.detail
        from specivo.models.journal import Journal

        journals = (await db_session.execute(select(Journal).where(Journal.issue_id == world.issue.id))).scalars()
        status_journal = [j for j in journals if j.notes and j.notes.startswith("Fixed in commit")]
        assert len(status_journal) == 1
        assert status_journal[0].user_id == world.dev.id

    async def test_fixes_on_a_feature_branch_does_nothing(self, client, db_session, world, queued):
        integration = await _integration(db_session, world)
        result = await _push(
            client, db_session, integration, [commit("f2", "Fixes CMD-1", email=DEV_EMAIL)], queued, branch="feat/x"
        )
        assert await _status(db_session, world.issue) == world.new.id
        assert "not_default_branch" in result.detail

    async def test_matched_user_without_edit_issues(self, client, db_session, world, queued):
        world.role.permissions = ["view_issues", "log_time"]
        await db_session.commit()
        integration = await _integration(db_session, world)
        result = await _push(client, db_session, integration, [commit("f3", "Fixes CMD-1", email=DEV_EMAIL)], queued)
        assert await _status(db_session, world.issue) == world.new.id
        assert "missing_permission:edit_issues" in result.detail

    async def test_disallowed_transition(self, client, db_session, world, queued):
        other = Role(name="Cmd Other", permissions=["edit_issues"])
        db_session.add(other)
        await db_session.flush()
        db_session.add(
            WorkflowTransition(
                tracker_id=world.tracker.id,
                role_id=other.id,
                old_status_id=world.new.id,
                new_status_id=world.fixed.id,
            )
        )
        await db_session.commit()
        await WorkflowService().invalidate_cache()
        integration = await _integration(db_session, world)
        result = await _push(client, db_session, integration, [commit("f4", "Fixes CMD-1", email=DEV_EMAIL)], queued)
        assert await _status(db_session, world.issue) == world.new.id
        assert "transition_not_allowed" in result.detail

    async def test_no_matched_user(self, client, db_session, world, queued):
        integration = await _integration(db_session, world)
        result = await _push(
            client, db_session, integration, [commit("f5", "Fixes CMD-1", email="stranger@example.com")], queued
        )
        assert await _status(db_session, world.issue) == world.new.id
        assert "no_matched_user" in result.detail

    async def test_commands_disabled(self, client, db_session, world, queued):
        integration = await _integration(db_session, world, commands_enabled=False, time_logging_enabled=False)
        result = await _push(
            client, db_session, integration, [commit("f6", "Fixes CMD-1 @2h", email=DEV_EMAIL)], queued
        )
        assert await _status(db_session, world.issue) == world.new.id
        assert await _entries(db_session, world.issue) == []
        assert "commands_disabled" in result.detail

    async def test_redelivery_moves_once(self, client, db_session, world, queued):
        integration = await _integration(db_session, world)
        c = commit("f7", "Fixes CMD-1", email=DEV_EMAIL)
        await _push(client, db_session, integration, [c], queued)
        world.issue.status_id = world.new.id  # someone reopens it
        await db_session.commit()
        await _push(client, db_session, integration, [c], queued)
        assert await _status(db_session, world.issue) == world.new.id


class TestTimeLogging:
    async def test_time_entry_for_matched_user(self, client, db_session, world, queued):
        integration = await _integration(db_session, world)
        result = await _push(
            client, db_session, integration, [commit("t1", "CMD-1 @1h30m tidy the form", email=DEV_EMAIL)], queued
        )
        (entry,) = await _entries(db_session, world.issue)
        assert entry.user_id == world.dev.id
        assert entry.hours == Decimal("1.50")
        # Committed 2026-10-30T10:00+07:00, the user's timezone is Asia/Bangkok.
        assert entry.spent_on == date(2026, 10, 30)
        assert entry.comments.startswith("CMD-1 @1h30m tidy the form (")
        assert "CMD-1 time: logged" in result.detail

    async def test_time_on_any_branch(self, client, db_session, world, queued):
        integration = await _integration(db_session, world)
        await _push(
            client, db_session, integration, [commit("t2", "CMD-1 @2h", email=DEV_EMAIL)], queued, branch="feat/x"
        )
        assert [e.hours for e in await _entries(db_session, world.issue)] == [Decimal("2.00")]

    async def test_redelivery_still_one_entry(self, client, db_session, world, queued):
        integration = await _integration(db_session, world)
        c = commit("t3", "CMD-1 @2h", email=DEV_EMAIL)
        await _push(client, db_session, integration, [c], queued, delivery="first")
        await _push(client, db_session, integration, [c], queued, delivery="second")
        assert len(await _entries(db_session, world.issue)) == 1

    async def test_unverified_email_logs_nothing(self, client, db_session, world, queued):
        world.dev.email_verified_at = None
        await db_session.commit()
        integration = await _integration(db_session, world)
        result = await _push(client, db_session, integration, [commit("t4", "CMD-1 @2h", email=DEV_EMAIL)], queued)
        assert await _entries(db_session, world.issue) == []
        assert "no_matched_user" in result.detail

    async def test_without_log_time_permission(self, client, db_session, world, queued):
        world.role.permissions = ["view_issues", "edit_issues"]
        await db_session.commit()
        integration = await _integration(db_session, world)
        result = await _push(client, db_session, integration, [commit("t5", "CMD-1 @2h", email=DEV_EMAIL)], queued)
        assert await _entries(db_session, world.issue) == []
        assert "missing_permission:log_time" in result.detail

    async def test_time_logging_disabled(self, client, db_session, world, queued):
        integration = await _integration(db_session, world, time_logging_enabled=False)
        result = await _push(client, db_session, integration, [commit("t6", "CMD-1 @2h", email=DEV_EMAIL)], queued)
        assert await _entries(db_session, world.issue) == []
        assert "time_logging_disabled" in result.detail

    async def test_fix_and_time_in_one_commit(self, client, db_session, world, queued):
        integration = await _integration(db_session, world)
        await _push(client, db_session, integration, [commit("t7", "Fixes CMD-1 @45m", email=DEV_EMAIL)], queued)
        assert await _status(db_session, world.issue) == world.fixed.id
        assert [e.hours for e in await _entries(db_session, world.issue)] == [Decimal("0.75")]
